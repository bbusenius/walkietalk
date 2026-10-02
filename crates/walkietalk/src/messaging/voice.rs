//! Converting voice notes with ffmpeg.

use std::path::Path;
use std::time::Duration;

use anyhow::{Context, bail};

use crate::audio::{Clip, Fit, TooLong};
use crate::exec::{self, Job};

/// Longest incoming note that will be transcribed.
pub const MAX_TRANSCRIBE: Duration = Duration::from_secs(300);
/// Largest outgoing voice note.
pub const MAX_UPLOAD_BYTES: u64 = 1024 * 1024;
const FFMPEG_TIMEOUT: Duration = Duration::from_secs(30);

fn env() -> std::collections::HashMap<String, String> {
    exec::inherit(&["HOME", "PATH", "LANG", "LC_ALL"])
}

/// Decode any audio file to mono 16-bit at `rate`, refusing (or cropping)
/// audio longer than `max`. Decoding stops one sample past the limit, so a
/// huge file is never fully decoded.
pub async fn decode(path: &Path, rate: u32, max: Duration, fit: Fit) -> anyhow::Result<Clip> {
    let limit = (max.as_secs_f64() * rate as f64) as usize;
    anyhow::ensure!(limit > 0, "no room for the voice message");
    let dir = tempfile::Builder::new().prefix("walkietalk-voice-").tempdir()?;
    let out = dir.path().join("voice.wav");
    let ffmpeg = exec::find("ffmpeg").context("ffmpeg is required for voice messages")?;
    let output = Job::new(ffmpeg, "ffmpeg")
        .args(["-nostdin", "-v", "error", "-y", "-i"])
        .arg(path.as_os_str())
        .args(["-af".to_string(), format!("aresample={rate},atrim=end_sample={}", limit + 1)])
        .args(["-ac".to_string(), "1".into(), "-ar".into(), rate.to_string(), "-c:a".into(), "pcm_s16le".into()])
        .arg(out.as_os_str())
        .env(env())
        .cwd(dir.path())
        .timeout(FFMPEG_TIMEOUT)
        .max_output(64 * 1024)
        .run()
        .await?;
    if !output.success() {
        bail!("cannot decode the voice message");
    }
    let clip = Clip::read_wav(&out, Duration::from_secs_f64((limit + 1) as f64 / rate as f64))?;
    if clip.len() > limit && fit == Fit::Strict {
        bail!(TooLong { actual: clip.duration(), limit: max });
    }
    Ok(clip.fit(max, Fit::Crop)?)
}

/// Encode captured speech as an Ogg/Opus voice note in `dir`.
pub async fn encode(clip: &Clip, dir: &Path) -> anyhow::Result<std::path::PathBuf> {
    let source = dir.join("voice.wav");
    let out = dir.join("voice.ogg");
    clip.write_new_wav(&source)?;
    let ffmpeg = exec::find("ffmpeg").context("ffmpeg is required for voice messages")?;
    let output = Job::new(ffmpeg, "ffmpeg")
        .args(["-nostdin", "-v", "error", "-y", "-i"])
        .arg(source.as_os_str())
        .args(["-ac", "1", "-c:a", "libopus", "-b:a", "32k", "-application", "voip"])
        .arg(out.as_os_str())
        .env(env())
        .cwd(dir)
        .timeout(FFMPEG_TIMEOUT)
        .max_output(64 * 1024)
        .run()
        .await?;
    let size = std::fs::metadata(&out).map(|m| m.len()).unwrap_or(0);
    if !output.success() || size == 0 {
        bail!("cannot encode the voice message (ffmpeg needs the libopus encoder)");
    }
    anyhow::ensure!(size <= MAX_UPLOAD_BYTES, "the voice message is larger than 1 MiB");
    Ok(out)
}
