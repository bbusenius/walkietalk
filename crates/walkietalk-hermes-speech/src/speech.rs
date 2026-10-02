//! Generating speech in Hermes's Python and converting it for the radio.

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use serde::Deserialize;
use tokio::io::AsyncReadExt;
use tokio::process::{Child, Command};

const RATE: u32 = 48_000;
/// Largest file the provider may produce.
const MAX_SOURCE_BYTES: u64 = 64 * 1024 * 1024;
const MAX_TEXT_CHARS: usize = 2000;

/// The only code that runs in Hermes's Python: ask its TTS tool for audio
/// with the configured provider, and refuse any substituted provider.
const SNIPPET: &str = r#"
import json, sys
from pathlib import Path
root, request, out = sys.argv[1], json.loads(Path(sys.argv[2]).read_text()), Path(sys.argv[3])
sys.path.insert(0, root)
from hermes_constants import get_hermes_home
try:
    from dotenv import load_dotenv
    load_dotenv(Path(get_hermes_home()) / ".env", override=False)
except ImportError:
    pass
from hermes_cli.config import load_config
from tools.tts_tool import BUILTIN_TTS_PROVIDERS, text_to_speech_tool
config = load_config().get("tts", {}) or {}
provider = str(config.get("provider", "")).strip().lower()
custom = (config.get("providers") or {}).get(provider, {}) or {}
if not provider or (provider not in BUILTIN_TTS_PROVIDERS and custom.get("type") != "command"):
    sys.exit(2)
result = json.loads(text_to_speech_tool(request["text"], str(out / "source.mp3")))
if result.get("success") is not True or result.get("provider") != provider:
    sys.exit(3)
print(json.dumps({"file": result.get("file_path", "")}))
"#;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub text: String,
    /// Longest audio wanted.
    pub max_seconds: f64,
    /// Cut longer speech (true) or refuse it (false).
    pub crop: bool,
}

impl Request {
    pub fn validate(&self, max_audio: f64) -> Result<(), ()> {
        let text_ok = !self.text.trim().is_empty()
            && self.text.chars().count() <= MAX_TEXT_CHARS
            && !self.text.chars().any(char::is_control);
        let limit_ok =
            self.max_seconds.is_finite() && self.max_seconds > 0.0 && self.max_seconds <= max_audio;
        if text_ok && limit_ok { Ok(()) } else { Err(()) }
    }
}

pub struct Limits {
    pub hermes_root: PathBuf,
    pub python: PathBuf,
    pub max_audio: f64,
    pub timeout: Duration,
}

#[derive(Debug)]
pub enum SpeechError {
    TimedOut,
    TooLong,
    Failed(&'static str),
}

/// Kills a child's whole process group when dropped.
struct Group(Option<i32>);

impl Group {
    fn of(child: &Child) -> Group {
        Group(child.id().map(|id| id as i32))
    }
}

impl Drop for Group {
    fn drop(&mut self) {
        if let Some(pgid) = self.0 {
            // SAFETY: killpg only sends a signal; a stale group is harmless.
            unsafe {
                libc::killpg(pgid, libc::SIGKILL);
            }
        }
    }
}

/// Run a program in its own process group, keeping a bounded stdout.
async fn run(mut command: Command, max_stdout: usize) -> Result<(bool, Vec<u8>), SpeechError> {
    command
        .process_group(0)
        .kill_on_drop(true)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    let mut child = command
        .spawn()
        .map_err(|_| SpeechError::Failed("cannot start a helper program"))?;
    let _group = Group::of(&child);
    let mut stdout = child.stdout.take().expect("piped");
    let mut out = Vec::new();
    let mut buf = [0u8; 4096];
    loop {
        let n = stdout
            .read(&mut buf)
            .await
            .map_err(|_| SpeechError::Failed("helper output failed"))?;
        if n == 0 {
            break;
        }
        if out.len() + n > max_stdout {
            return Err(SpeechError::Failed("helper output too large"));
        }
        out.extend_from_slice(&buf[..n]);
    }
    let status = child
        .wait()
        .await
        .map_err(|_| SpeechError::Failed("helper failed"))?;
    Ok((status.success(), out))
}

pub async fn generate(request: &Request, limits: &Limits) -> Result<Vec<u8>, SpeechError> {
    match tokio::time::timeout(limits.timeout, produce(request, limits)).await {
        Ok(result) => result,
        Err(_) => Err(SpeechError::TimedOut),
    }
}

async fn produce(request: &Request, limits: &Limits) -> Result<Vec<u8>, SpeechError> {
    let dir = tempfile::Builder::new()
        .prefix("walkietalk-hermes-speech-")
        .tempdir()
        .map_err(|_| SpeechError::Failed("cannot create a working directory"))?;
    let request_path = dir.path().join("request.json");
    std::fs::write(
        &request_path,
        serde_json::json!({"text": request.text}).to_string(),
    )
    .map_err(|_| SpeechError::Failed("cannot write the request"))?;
    let mut python = Command::new(&limits.python);
    python
        .arg("-c")
        .arg(SNIPPET)
        .arg(&limits.hermes_root)
        .arg(&request_path)
        .arg(dir.path())
        .current_dir(dir.path());
    let (ok, stdout) = run(python, 64 * 1024).await?;
    if !ok {
        return Err(SpeechError::Failed(
            "the configured provider failed or was not explicitly selected",
        ));
    }
    let reply: serde_json::Value = serde_json::from_slice(&stdout)
        .map_err(|_| SpeechError::Failed("invalid provider reply"))?;
    let source = reply["file"]
        .as_str()
        .map(PathBuf::from)
        .ok_or(SpeechError::Failed("no audio file"))?;
    let source = source
        .canonicalize()
        .map_err(|_| SpeechError::Failed("audio file missing"))?;
    let inside = dir
        .path()
        .canonicalize()
        .map(|d| source.starts_with(d))
        .unwrap_or(false);
    let size = std::fs::metadata(&source).map(|m| m.len()).unwrap_or(0);
    if !inside || size == 0 || size > MAX_SOURCE_BYTES {
        return Err(SpeechError::Failed("invalid audio file"));
    }
    convert(&source, dir.path(), request).await
}

/// Decode to 48 kHz mono 16-bit. A strict request keeps one extra sample so
/// overlong speech is detected instead of silently shortened.
async fn convert(source: &Path, dir: &Path, request: &Request) -> Result<Vec<u8>, SpeechError> {
    let limit = (request.max_seconds * RATE as f64).floor() as u64;
    let keep = if request.crop { limit } else { limit + 1 };
    let out = dir.join("speech.wav");
    let mut ffmpeg = Command::new("ffmpeg");
    ffmpeg
        .args(["-nostdin", "-v", "error", "-y", "-i"])
        .arg(source)
        .arg("-af")
        .arg(format!("aresample={RATE},atrim=end_sample={keep}"))
        .args(["-ac", "1", "-ar", &RATE.to_string(), "-c:a", "pcm_s16le"])
        .arg(&out)
        .current_dir(dir);
    let (ok, _) = run(ffmpeg, 64 * 1024).await?;
    if !ok {
        return Err(SpeechError::Failed("audio conversion failed"));
    }
    let reader =
        hound::WavReader::open(&out).map_err(|_| SpeechError::Failed("invalid converted audio"))?;
    let spec = reader.spec();
    if spec.channels != 1 || spec.sample_rate != RATE || spec.bits_per_sample != 16 {
        return Err(SpeechError::Failed("invalid converted audio"));
    }
    let frames = reader.duration() as u64;
    if frames == 0 {
        return Err(SpeechError::Failed("empty audio"));
    }
    if frames > limit {
        return Err(SpeechError::TooLong);
    }
    std::fs::read(&out).map_err(|_| SpeechError::Failed("cannot read the converted audio"))
}
