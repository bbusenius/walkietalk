//! `check`: confirm the configured devices and backends are ready,
//! without opening streams, keying the radio, or making requests.

use std::ffi::CString;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::FileTypeExt;
use std::path::Path;

use crate::audio::device::{self, Direction};
use crate::config::{AgentBackend, Config, SttBackend, TtsBackend};
use crate::credentials::Credentials;
use crate::gate::Gate;
use crate::grok_login::GrokLogin;
use crate::{backends, exec, stt, ui};

struct Report {
    problems: usize,
}

impl Report {
    fn line(&mut self, what: &str, result: anyhow::Result<String>) {
        match result {
            Ok(detail) => ui::status!("ok    {what}: {detail}"),
            Err(err) => {
                self.problems += 1;
                ui::error!("FAIL  {what}: {err:#}");
            }
        }
    }
}

fn serial(path: &Path) -> anyhow::Result<String> {
    let meta = std::fs::metadata(path).map_err(|_| anyhow::anyhow!("{} not found; connect the interface", path.display()))?;
    anyhow::ensure!(meta.file_type().is_char_device(), "{} is not a serial device", path.display());
    let c_path = CString::new(path.as_os_str().as_bytes())?;
    // SAFETY: access only reads the NUL-terminated path.
    let ok = unsafe { libc::access(c_path.as_ptr(), libc::R_OK | libc::W_OK) } == 0;
    anyhow::ensure!(ok, "no read/write permission for {}; see the serial permissions section of the install guide", path.display());
    Ok(format!("{} (not opened)", path.display()))
}

fn program(name: &str) -> anyhow::Result<String> {
    exec::find(name).map(|p| p.display().to_string()).map_err(Into::into)
}

pub fn run(config: &Config, creds: &Credentials) -> anyhow::Result<()> {
    let mut r = Report { problems: 0 };
    r.line("capture device", device::find(&config.audio.input, Direction::Input).map(|_| config.audio.input.clone()));
    r.line("playback device", device::find(&config.audio.output, Direction::Output).map(|_| config.audio.output.clone()));
    r.line(&format!("PTT ({} keys)", config.ptt.line), serial(&config.ptt.port));

    if config.agent.backend.is_realtime() {
        let rt = &config.agent.realtime;
        r.line(
            "voice agent",
            creds.token(&rt.key_env).map(|_| format!("grok-realtime {} voice {}; {} set", rt.model, rt.voice, rt.key_env)),
        );
    } else {
        let stt_ready = match config.stt.backend {
            SttBackend::Whisper => stt::models::installed(config.stt.model)
                .map(|bytes| format!("whisper {} ({} MB)", config.stt.model, bytes / 1_000_000))
                .ok_or_else(|| anyhow::anyhow!("Whisper {} is not downloaded; run `walkietalk models`", config.stt.model)),
            SttBackend::Grok => GrokLogin::locate().check().map(|_| "grok with the saved Grok login".into()),
            SttBackend::GrokApi => creds.token(&config.stt.api_key_env).map(|_| format!("grok-api; {} set", config.stt.api_key_env)),
        };
        r.line("speech recognition", stt_ready);
        let a = &config.agent;
        let agent_ready = backends::text_agent(config, creds).and_then(|agent| {
            match a.backend {
                AgentBackend::Hermes => creds.token(&a.hermes.token_env).map(|_| ()),
                AgentBackend::Codex => program(&a.codex.executable).map(|_| ()),
                AgentBackend::Grok => program(&a.grok.executable).and_then(|_| GrokLogin::locate().check()),
                AgentBackend::Claude => program(&a.claude.executable).map(|_| ()),
                AgentBackend::ClaudeApi => creds.token(&a.claude_api.key_env).map(|_| ()),
                AgentBackend::Stub | AgentBackend::GrokRealtime => Ok(()),
            }
            .map(|()| agent.label())
        });
        r.line("agent", agent_ready);
    }
    if !config.agent.backend.is_realtime() || config.messaging_enabled() {
        let t = &config.tts;
        let voice_ready = backends::voice(config, creds).and_then(|voice| {
            match t.backend {
                TtsBackend::Piper => program(&t.piper.executable).and_then(|_| {
                    let model = config.piper_model();
                    anyhow::ensure!(model.is_file(), "Piper voice {} not found", model.display());
                    Ok(())
                }),
                TtsBackend::Grok => GrokLogin::locate().check(),
                TtsBackend::GrokApi => creds.token(&t.grok.key_env).map(|_| ()),
                TtsBackend::Hermes => creds.token(&t.hermes.token_env).map(|_| ()),
            }
            .map(|()| voice.label())
        });
        r.line("voice", voice_ready);
    }
    for (service, contact) in config.messaging.enabled() {
        let tool = match service {
            crate::config::Service::WhatsApp => "wacli",
            crate::config::Service::Signal => "signal-cli",
        };
        r.line(&format!("{service} ({tool})"), program(tool).map(|p| format!("{p}; wake \"{}\"", contact.wake)));
        r.line("ffmpeg", program("ffmpeg"));
    }

    ui::status!("{}", Gate::new(config).status(std::time::Instant::now()));
    if let Some(sleep) = &config.sleep {
        ui::status!("Sleep phrase: \"{}\"", sleep.phrase);
    }
    if config.shutdown.enabled {
        ui::status!("Remote shutdown: enabled");
    }
    let id = &config.radio.station_id;
    if id.enabled() {
        ui::status!("Station ID: {} ({:?}, {:?})", id.callsign, id.mode, id.method);
    } else {
        ui::status!("Station ID: off");
    }
    ui::status!("Nothing was opened or transmitted.");
    anyhow::ensure!(r.problems == 0, "{} problem(s) found", r.problems);
    Ok(())
}
