//! The settings file: schema, defaults, and validation.
//!
//! Every option lives in one TOML file. Sections for backends that are not
//! selected may be omitted. [`Config::load`] parses the file and runs every
//! check up front, so later code can rely on validated values.

use std::collections::BTreeMap;
use std::fmt;
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde::Deserialize;

use crate::phrases::{self, Phrase};

/// Rate the radio path uses for every outgoing clip.
pub const RADIO_RATE: u32 = 48_000;

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub audio: AudioConfig,
    pub ptt: PttConfig,
    #[serde(default)]
    pub radio: RadioConfig,
    #[serde(default)]
    pub vad: VadConfig,
    #[serde(default)]
    pub stt: SttConfig,
    #[serde(default)]
    pub listening: ListeningConfig,
    pub wake: WakeConfig,
    pub sleep: Option<SleepConfig>,
    #[serde(default)]
    pub shutdown: ShutdownConfig,
    #[serde(default)]
    pub agent: AgentConfig,
    #[serde(default)]
    pub tts: TtsConfig,
    #[serde(default)]
    pub messaging: MessagingConfig,
    /// Directory holding the config file; relative paths resolve against it.
    #[serde(skip)]
    pub base_dir: PathBuf,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AudioConfig {
    /// Capture device name from `walkietalk devices`.
    pub input: String,
    /// Playback device name from `walkietalk devices`.
    pub output: String,
    /// Outgoing amplitude multiplier.
    #[serde(default = "one")]
    pub gain: f64,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PttConfig {
    /// Serial device that keys the radio, ideally a `/dev/serial/by-id/` path.
    pub port: PathBuf,
    /// Modem line that keys the radio. The other line is held low.
    #[serde(default)]
    pub line: PttLine,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum PttLine {
    #[default]
    Dtr,
    Rts,
}

impl fmt::Display for PttLine {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            PttLine::Dtr => "DTR",
            PttLine::Rts => "RTS",
        })
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct RadioConfig {
    /// Hard limit for one transmission, including the settle time.
    pub max_tx_seconds: f64,
    /// Delay between keying and audio, so the first word is not clipped.
    pub settle_seconds: f64,
    /// Listening pause after the transmitter is released.
    pub post_tx_mute_seconds: f64,
    pub station_id: StationIdConfig,
}

impl Default for RadioConfig {
    fn default() -> Self {
        Self {
            max_tx_seconds: 10.0,
            settle_seconds: 0.2,
            post_tx_mute_seconds: 2.0,
            station_id: StationIdConfig::default(),
        }
    }
}

impl RadioConfig {
    pub fn max_tx(&self) -> Duration {
        Duration::from_secs_f64(self.max_tx_seconds)
    }

    pub fn settle(&self) -> Duration {
        Duration::from_secs_f64(self.settle_seconds)
    }

    pub fn post_tx_mute(&self) -> Duration {
        Duration::from_secs_f64(self.post_tx_mute_seconds)
    }

    /// Audio that fits in one transmission after the settle delay.
    pub fn speech_budget(&self) -> Duration {
        self.max_tx().saturating_sub(self.settle())
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct StationIdConfig {
    /// Your granted station ID; empty disables identification.
    pub callsign: String,
    pub mode: StationIdMode,
    pub method: StationIdMethod,
    pub interval_seconds: f64,
}

impl Default for StationIdConfig {
    fn default() -> Self {
        Self {
            callsign: String::new(),
            mode: StationIdMode::EndOfReply,
            method: StationIdMethod::Voice,
            interval_seconds: 900.0,
        }
    }
}

impl StationIdConfig {
    pub fn enabled(&self) -> bool {
        !self.callsign.trim().is_empty() && self.mode != StationIdMode::Off
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum StationIdMode {
    Off,
    EndOfReply,
    Interval,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum StationIdMethod {
    Voice,
    Morse,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct VadConfig {
    /// RMS level (0 to 1) that starts an utterance.
    pub threshold: f64,
    /// Silence that ends an utterance.
    pub hangover_ms: u32,
    /// Longest single recording.
    pub max_utterance_seconds: f64,
}

impl Default for VadConfig {
    fn default() -> Self {
        Self {
            threshold: 0.02,
            hangover_ms: 400,
            max_utterance_seconds: 12.0,
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct SttConfig {
    pub backend: SttBackend,
    /// Local Whisper model size.
    pub model: WhisperModel,
    pub timeout_seconds: f64,
    /// Cap on each remote response body.
    pub max_response_bytes: usize,
    /// Variable holding the billed xAI key for `grok-api`.
    pub api_key_env: String,
}

impl Default for SttConfig {
    fn default() -> Self {
        Self {
            backend: SttBackend::Whisper,
            model: WhisperModel::Base,
            timeout_seconds: 30.0,
            max_response_bytes: 1024 * 1024,
            api_key_env: "XAI_API_KEY".into(),
        }
    }
}

impl SttConfig {
    pub fn timeout(&self) -> Duration {
        Duration::from_secs_f64(self.timeout_seconds)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum SttBackend {
    /// Local whisper.cpp.
    Whisper,
    /// xAI speech-to-text with the Grok CLI's saved login.
    Grok,
    /// xAI speech-to-text with a billed API key.
    GrokApi,
}

impl fmt::Display for SttBackend {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            SttBackend::Whisper => "whisper",
            SttBackend::Grok => "grok",
            SttBackend::GrokApi => "grok-api",
        })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum WhisperModel {
    Tiny,
    Base,
    Small,
}

impl fmt::Display for WhisperModel {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            WhisperModel::Tiny => "tiny",
            WhisperModel::Base => "base",
            WhisperModel::Small => "small",
        })
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ListeningConfig {
    pub mode: ListeningMode,
    /// How long unaddressed follow-ups are accepted after a reply.
    pub follow_up_seconds: f64,
}

impl Default for ListeningConfig {
    fn default() -> Self {
        Self {
            mode: ListeningMode::Conversation,
            follow_up_seconds: 30.0,
        }
    }
}

impl ListeningConfig {
    fn messaging_default() -> Self {
        Self {
            mode: ListeningMode::Conversation,
            follow_up_seconds: 60.0,
        }
    }

    pub fn follow_up(&self) -> Duration {
        Duration::from_secs_f64(self.follow_up_seconds)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ListeningMode {
    /// Every request starts with the wake name.
    WakePhrase,
    /// The wake name opens a follow-up window.
    Conversation,
}

impl fmt::Display for ListeningMode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            ListeningMode::WakePhrase => "wake-phrase",
            ListeningMode::Conversation => "conversation",
        })
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WakeConfig {
    pub name: String,
    /// Common mistranscriptions of the name.
    #[serde(default)]
    pub aliases: Vec<String>,
    /// Spoken when the name arrives with no request; empty stays silent.
    #[serde(default)]
    pub confirmation: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SleepConfig {
    pub phrase: String,
    #[serde(default)]
    pub aliases: Vec<String>,
    #[serde(default)]
    pub confirmation: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ShutdownConfig {
    pub enabled: bool,
    pub phrase: String,
    pub phrase_aliases: Vec<String>,
    pub code: String,
    pub code_aliases: Vec<String>,
    /// How long after the phrase the code is accepted.
    pub confirm_window_seconds: f64,
    /// Spoken when the phrase alone arms shutdown.
    pub armed_reply: String,
    /// Spoken after the code is accepted, before exiting.
    pub confirmed_reply: String,
}

impl Default for ShutdownConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            phrase: String::new(),
            phrase_aliases: Vec::new(),
            code: String::new(),
            code_aliases: Vec::new(),
            confirm_window_seconds: 30.0,
            armed_reply: String::new(),
            confirmed_reply: String::new(),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct AgentConfig {
    pub backend: AgentBackend,
    pub max_reply_chars: usize,
    /// Completed request/reply pairs kept as context.
    pub history_turns: usize,
    pub timeout_seconds: f64,
    pub web_search: bool,
    /// Replaces the built-in guidance. Placeholders: {max_reply_chars},
    /// {spoken_seconds}, {max_words}.
    pub instructions: String,
    pub hermes: HermesAgentConfig,
    pub codex: CliAgentConfig,
    pub grok: CliAgentConfig,
    pub claude: CliAgentConfig,
    pub claude_api: ClaudeApiConfig,
    pub realtime: RealtimeConfig,
}

impl Default for AgentConfig {
    fn default() -> Self {
        Self {
            backend: AgentBackend::Stub,
            max_reply_chars: 600,
            history_turns: 8,
            timeout_seconds: 60.0,
            web_search: true,
            instructions: String::new(),
            hermes: HermesAgentConfig::default(),
            codex: CliAgentConfig::named("codex"),
            grok: CliAgentConfig::named("grok"),
            claude: CliAgentConfig::named("claude"),
            claude_api: ClaudeApiConfig::default(),
            realtime: RealtimeConfig::default(),
        }
    }
}

impl AgentConfig {
    pub fn timeout(&self) -> Duration {
        Duration::from_secs_f64(self.timeout_seconds)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum AgentBackend {
    Stub,
    Hermes,
    Codex,
    Grok,
    Claude,
    ClaudeApi,
    GrokRealtime,
}

impl AgentBackend {
    pub fn is_realtime(self) -> bool {
        self == AgentBackend::GrokRealtime
    }
}

impl fmt::Display for AgentBackend {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            AgentBackend::Stub => "stub",
            AgentBackend::Hermes => "hermes",
            AgentBackend::Codex => "codex",
            AgentBackend::Grok => "grok",
            AgentBackend::Claude => "claude",
            AgentBackend::ClaudeApi => "claude-api",
            AgentBackend::GrokRealtime => "grok-realtime",
        })
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct HermesAgentConfig {
    /// Hermes API base URL, without `/v1`.
    pub url: String,
    pub token_env: String,
}

impl Default for HermesAgentConfig {
    fn default() -> Self {
        Self {
            url: "http://127.0.0.1:8642".into(),
            token_env: "WALKIETALK_HERMES_TOKEN".into(),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct CliAgentConfig {
    /// Executable name on PATH or an absolute path.
    pub executable: String,
    /// Empty uses the CLI's default model.
    pub model: String,
    pub reasoning_effort: ReasoningEffort,
}

impl CliAgentConfig {
    fn named(executable: &str) -> Self {
        Self {
            executable: executable.into(),
            model: String::new(),
            reasoning_effort: ReasoningEffort::Low,
        }
    }
}

impl Default for CliAgentConfig {
    fn default() -> Self {
        Self::named("")
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ClaudeApiConfig {
    pub key_env: String,
    pub model: String,
    pub reasoning_effort: ReasoningEffort,
}

impl Default for ClaudeApiConfig {
    fn default() -> Self {
        Self {
            key_env: "ANTHROPIC_API_KEY".into(),
            model: String::new(),
            reasoning_effort: ReasoningEffort::Low,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ReasoningEffort {
    /// Leave the model's own default in place.
    Default,
    None,
    Minimal,
    Low,
    Medium,
    High,
    Xhigh,
    Max,
}

impl ReasoningEffort {
    pub fn as_str(self) -> &'static str {
        match self {
            ReasoningEffort::Default => "default",
            ReasoningEffort::None => "none",
            ReasoningEffort::Minimal => "minimal",
            ReasoningEffort::Low => "low",
            ReasoningEffort::Medium => "medium",
            ReasoningEffort::High => "high",
            ReasoningEffort::Xhigh => "xhigh",
            ReasoningEffort::Max => "max",
        }
    }

    /// The explicit value to pass to a backend, if any.
    pub fn explicit(self) -> Option<&'static str> {
        (self != ReasoningEffort::Default).then(|| self.as_str())
    }
}

impl fmt::Display for ReasoningEffort {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct RealtimeConfig {
    pub model: String,
    pub voice: String,
    pub key_env: String,
    pub url: String,
    pub connect_timeout_seconds: f64,
    pub idle_timeout_seconds: f64,
}

impl Default for RealtimeConfig {
    fn default() -> Self {
        Self {
            model: "grok-voice-latest".into(),
            voice: "eve".into(),
            key_env: "XAI_API_KEY".into(),
            url: "wss://api.x.ai/v1/realtime".into(),
            connect_timeout_seconds: 10.0,
            idle_timeout_seconds: 60.0,
        }
    }
}

impl RealtimeConfig {
    pub fn connect_timeout(&self) -> Duration {
        Duration::from_secs_f64(self.connect_timeout_seconds)
    }

    pub fn idle_timeout(&self) -> Duration {
        Duration::from_secs_f64(self.idle_timeout_seconds)
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct TtsConfig {
    pub backend: TtsBackend,
    pub timeout_seconds: f64,
    /// Scale synthesized speech so its loudest peak reaches full scale.
    pub peak_normalize: bool,
    pub piper: PiperConfig,
    pub grok: GrokVoiceConfig,
    pub hermes: HermesSpeechConfig,
}

impl Default for TtsConfig {
    fn default() -> Self {
        Self {
            backend: TtsBackend::Piper,
            timeout_seconds: 30.0,
            peak_normalize: false,
            piper: PiperConfig::default(),
            grok: GrokVoiceConfig::default(),
            hermes: HermesSpeechConfig::default(),
        }
    }
}

impl TtsConfig {
    pub fn timeout(&self) -> Duration {
        Duration::from_secs_f64(self.timeout_seconds)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum TtsBackend {
    Piper,
    Grok,
    GrokApi,
    Hermes,
}

impl fmt::Display for TtsBackend {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            TtsBackend::Piper => "piper",
            TtsBackend::Grok => "grok",
            TtsBackend::GrokApi => "grok-api",
            TtsBackend::Hermes => "hermes",
        })
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct PiperConfig {
    pub executable: String,
    /// Voice `.onnx` file; its `.onnx.json` must sit beside it.
    pub model: PathBuf,
}

impl Default for PiperConfig {
    fn default() -> Self {
        Self {
            executable: "piper".into(),
            model: PathBuf::from("~/.local/share/walkietalk/piper/en_US-amy-medium.onnx"),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct GrokVoiceConfig {
    pub voice: String,
    pub language: String,
    pub speed: f64,
    /// Variable holding the billed key; used only by `grok-api`.
    pub key_env: String,
}

impl Default for GrokVoiceConfig {
    fn default() -> Self {
        Self {
            voice: "eve".into(),
            language: "en".into(),
            speed: 1.0,
            key_env: "XAI_API_KEY".into(),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct HermesSpeechConfig {
    /// Speech companion base URL.
    pub url: String,
    pub token_env: String,
}

impl Default for HermesSpeechConfig {
    fn default() -> Self {
        Self {
            url: "http://127.0.0.1:8643".into(),
            token_env: "WALKIETALK_HERMES_TOKEN".into(),
        }
    }
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct MessagingConfig {
    /// Hold every incoming and outgoing message for local review.
    pub operator_mode: bool,
    pub whatsapp: Option<ContactConfig>,
    pub signal: Option<ContactConfig>,
}

impl MessagingConfig {
    pub fn contact(&self, service: Service) -> Option<&ContactConfig> {
        match service {
            Service::WhatsApp => self.whatsapp.as_ref(),
            Service::Signal => self.signal.as_ref(),
        }
    }

    pub fn enabled(&self) -> impl Iterator<Item = (Service, &ContactConfig)> {
        Service::ALL
            .into_iter()
            .filter_map(|service| self.contact(service).map(|contact| (service, contact)))
    }

    pub fn any_enabled(&self) -> bool {
        self.enabled().next().is_some()
    }
}

/// A messaging service.
#[derive(
    Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, serde::Serialize, Deserialize,
)]
#[serde(rename_all = "lowercase")]
pub enum Service {
    #[serde(rename = "whatsapp")]
    WhatsApp,
    Signal,
}

impl Service {
    pub const ALL: [Service; 2] = [Service::WhatsApp, Service::Signal];
}

impl fmt::Display for Service {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Service::WhatsApp => "WhatsApp",
            Service::Signal => "Signal",
        })
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ContactConfig {
    /// Wake phrase that opens this contact's conversation.
    pub wake: String,
    #[serde(default)]
    pub aliases: Vec<String>,
    /// Destination number or ID.
    pub to: String,
    /// Spoken sender label; empty uses the wake phrase.
    #[serde(default)]
    pub sender_alias: String,
    /// Spoken when the wake phrase arrives alone and nothing is queued.
    #[serde(default)]
    pub empty_queue_reply: String,
    /// Send the original recording as a voice note instead of its transcript.
    #[serde(default)]
    pub send_as_voice: bool,
    /// Transcribe incoming voice notes and read them with the voice.
    #[serde(default)]
    pub transcribe_voice: bool,
    #[serde(default = "ListeningConfig::messaging_default")]
    pub listening: ListeningConfig,
    /// Signal only: local sending account in +country format.
    #[serde(default)]
    pub account: String,
    /// Signal only: where signal-cli stores received attachments.
    pub attachments_dir: Option<PathBuf>,
}

impl ContactConfig {
    /// Label spoken before a contact's message.
    pub fn label(&self) -> &str {
        if self.sender_alias.trim().is_empty() {
            self.wake.trim()
        } else {
            self.sender_alias.trim()
        }
    }
}

fn one() -> f64 {
    1.0
}

/// All problems found in a config file.
#[derive(Debug, thiserror::Error)]
#[error("{}", .problems.join("\n"))]
pub struct ConfigError {
    pub problems: Vec<String>,
}

impl Config {
    /// Read, parse, and validate a config file.
    pub fn load(path: &Path) -> anyhow::Result<Config> {
        let text = std::fs::read_to_string(path)
            .map_err(|err| anyhow::anyhow!("cannot read config {}: {err}", path.display()))?;
        let base = path
            .canonicalize()
            .ok()
            .and_then(|p| p.parent().map(Path::to_path_buf))
            .unwrap_or_else(|| PathBuf::from("."));
        Ok(Config::parse(&text, base)?)
    }

    /// Parse and validate config text whose relative paths resolve against `base_dir`.
    pub fn parse(text: &str, base_dir: PathBuf) -> Result<Config, ConfigError> {
        let mut config: Config = toml::from_str(text).map_err(|err| ConfigError {
            problems: vec![format!("config syntax: {}", err.message().trim())],
        })?;
        config.base_dir = base_dir;
        config.normalize();
        config.validate()?;
        Ok(config)
    }

    fn normalize(&mut self) {
        let trim = |s: &mut String| *s = s.trim().to_string();
        let trim_all = |v: &mut Vec<String>| v.iter_mut().for_each(|s| *s = s.trim().to_string());
        trim(&mut self.wake.name);
        trim_all(&mut self.wake.aliases);
        trim(&mut self.wake.confirmation);
        if let Some(sleep) = &mut self.sleep {
            trim(&mut sleep.phrase);
            trim_all(&mut sleep.aliases);
            trim(&mut sleep.confirmation);
        }
        let s = &mut self.shutdown;
        for field in [
            &mut s.phrase,
            &mut s.code,
            &mut s.armed_reply,
            &mut s.confirmed_reply,
        ] {
            trim(field);
        }
        trim_all(&mut s.phrase_aliases);
        trim_all(&mut s.code_aliases);
        trim(&mut self.radio.station_id.callsign);
        trim(&mut self.agent.instructions);
        for contact in [&mut self.messaging.whatsapp, &mut self.messaging.signal]
            .into_iter()
            .flatten()
        {
            trim(&mut contact.wake);
            trim_all(&mut contact.aliases);
            trim(&mut contact.to);
            trim(&mut contact.sender_alias);
            trim(&mut contact.empty_queue_reply);
            trim(&mut contact.account);
        }
    }

    /// Resolve a configured path: `~` is the home directory, and relative
    /// paths are relative to the config file.
    pub fn resolve_path(&self, path: &Path) -> PathBuf {
        let expanded = crate::paths::expand_home(path);
        if expanded.is_absolute() {
            expanded
        } else {
            self.base_dir.join(expanded)
        }
    }

    pub fn piper_model(&self) -> PathBuf {
        self.resolve_path(&self.tts.piper.model)
    }

    /// The sleep phrases, if sleep is configured.
    pub fn sleep_phrases(&self) -> Vec<&str> {
        match &self.sleep {
            Some(sleep) if !sleep.phrase.is_empty() => std::iter::once(sleep.phrase.as_str())
                .chain(sleep.aliases.iter().map(String::as_str))
                .collect(),
            _ => Vec::new(),
        }
    }

    pub fn sleep_confirmation(&self) -> &str {
        self.sleep.as_ref().map_or("", |s| s.confirmation.as_str())
    }

    /// Whether any messaging contact needs the speech voice even with realtime.
    pub fn messaging_enabled(&self) -> bool {
        self.messaging.any_enabled()
    }

    fn validate(&self) -> Result<(), ConfigError> {
        let mut v = Validator::default();
        self.check_audio(&mut v);
        self.check_radio(&mut v);
        self.check_vad_and_stt(&mut v);
        self.check_phrases(&mut v);
        self.check_agent(&mut v);
        self.check_tts(&mut v);
        self.check_messaging(&mut v);
        v.finish()
    }

    fn check_audio(&self, v: &mut Validator) {
        for (field, name) in [
            ("audio.input", &self.audio.input),
            ("audio.output", &self.audio.output),
        ] {
            if name.trim().is_empty() || name != name.trim() {
                v.fail(format!(
                    "{field} must be a device name from `walkietalk devices`"
                ));
            } else if is_router_device(name) {
                v.fail(format!(
                    "{field} must name the radio interface itself, not a default or sound-server device"
                ));
            }
        }
        v.finite_range("audio.gain", self.audio.gain, 0.0, 16.0, false);
        if !self.ptt.port.is_absolute() || !self.ptt.port.starts_with("/dev") {
            v.fail("ptt.port must be an absolute /dev path such as /dev/serial/by-id/...");
        }
    }

    fn check_radio(&self, v: &mut Validator) {
        let r = &self.radio;
        v.finite_range("radio.max_tx_seconds", r.max_tx_seconds, 0.0, 600.0, false);
        v.finite_range("radio.settle_seconds", r.settle_seconds, 0.0, 2.0, false);
        if r.settle_seconds.is_finite()
            && r.max_tx_seconds.is_finite()
            && r.settle_seconds >= r.max_tx_seconds
        {
            v.fail("radio.settle_seconds must be less than radio.max_tx_seconds");
        }
        v.finite_range(
            "radio.post_tx_mute_seconds",
            r.post_tx_mute_seconds,
            0.0,
            30.0,
            true,
        );
        let id = &r.station_id;
        if id.callsign.chars().count() > 64 || id.callsign.chars().any(char::is_control) {
            v.fail("radio.station_id.callsign must be your station ID, at most 64 characters");
        } else if !id.callsign.is_empty() && !id.callsign.chars().any(char::is_alphanumeric) {
            v.fail("radio.station_id.callsign must contain letters or digits");
        }
        if id.method == StationIdMethod::Morse
            && !id
                .callsign
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == ' ')
        {
            v.fail("radio.station_id.callsign may contain only letters, digits, and spaces with method = \"morse\"");
        }
        v.finite_range(
            "radio.station_id.interval_seconds",
            id.interval_seconds,
            0.0,
            1800.0,
            false,
        );
    }

    fn check_vad_and_stt(&self, v: &mut Validator) {
        v.finite_range("vad.threshold", self.vad.threshold, 0.0, 1.0, false);
        if !(1..=5000).contains(&self.vad.hangover_ms) {
            v.fail("vad.hangover_ms must be from 1 through 5000");
        }
        v.finite_range(
            "vad.max_utterance_seconds",
            self.vad.max_utterance_seconds,
            0.0,
            30.0,
            false,
        );
        v.finite_range(
            "stt.timeout_seconds",
            self.stt.timeout_seconds,
            0.0,
            120.0,
            false,
        );
        if self.stt.max_response_bytes == 0 || self.stt.max_response_bytes > 16 * 1024 * 1024 {
            v.fail("stt.max_response_bytes must be from 1 through 16777216");
        }
        v.env_name("stt.api_key_env", &self.stt.api_key_env);
        v.finite_range(
            "listening.follow_up_seconds",
            self.listening.follow_up_seconds,
            0.0,
            600.0,
            false,
        );
    }

    fn check_agent(&self, v: &mut Validator) {
        let a = &self.agent;
        if !(1..=2000).contains(&a.max_reply_chars) {
            v.fail("agent.max_reply_chars must be from 1 through 2000");
        }
        if !(1..=32).contains(&a.history_turns) {
            v.fail("agent.history_turns must be from 1 through 32");
        }
        v.finite_range(
            "agent.timeout_seconds",
            a.timeout_seconds,
            0.0,
            300.0,
            false,
        );
        if a.instructions.chars().count() > 2000
            || a.instructions.chars().any(|c| c.is_control() && c != '\n')
        {
            v.fail("agent.instructions must be printable text, at most 2000 characters");
        } else if let Err(err) = crate::agent::instructions::Template::parse(&a.instructions) {
            v.fail(format!("agent.instructions: {err}"));
        }
        v.http_url("agent.hermes.url", &a.hermes.url);
        v.env_name("agent.hermes.token_env", &a.hermes.token_env);
        for (name, cli, efforts) in [
            (
                "codex",
                &a.codex,
                &[
                    ReasoningEffort::Default,
                    ReasoningEffort::Minimal,
                    ReasoningEffort::Low,
                    ReasoningEffort::Medium,
                    ReasoningEffort::High,
                    ReasoningEffort::Xhigh,
                ][..],
            ),
            (
                "grok",
                &a.grok,
                &[
                    ReasoningEffort::Default,
                    ReasoningEffort::None,
                    ReasoningEffort::Minimal,
                    ReasoningEffort::Low,
                    ReasoningEffort::Medium,
                    ReasoningEffort::High,
                    ReasoningEffort::Xhigh,
                    ReasoningEffort::Max,
                ][..],
            ),
            (
                "claude",
                &a.claude,
                &[
                    ReasoningEffort::Default,
                    ReasoningEffort::Low,
                    ReasoningEffort::Medium,
                    ReasoningEffort::High,
                    ReasoningEffort::Xhigh,
                    ReasoningEffort::Max,
                ][..],
            ),
        ] {
            v.executable(&format!("agent.{name}.executable"), &cli.executable);
            v.model_id(&format!("agent.{name}.model"), &cli.model, true);
            if !efforts.contains(&cli.reasoning_effort) {
                v.fail(format!(
                    "agent.{name}.reasoning_effort must be one of: {}",
                    efforts
                        .iter()
                        .map(|e| e.as_str())
                        .collect::<Vec<_>>()
                        .join(", ")
                ));
            }
        }
        if a.backend == AgentBackend::Grok && a.grok.model.is_empty() {
            v.fail("agent.grok.model is required with backend = \"grok\"");
        }
        if a.backend == AgentBackend::Claude && a.claude.model.is_empty() {
            v.fail("agent.claude.model is required with backend = \"claude\"");
        }
        v.env_name("agent.claude_api.key_env", &a.claude_api.key_env);
        v.model_id("agent.claude_api.model", &a.claude_api.model, true);
        if a.backend == AgentBackend::ClaudeApi && a.claude_api.model.is_empty() {
            v.fail("agent.claude_api.model is required with backend = \"claude-api\"");
        }
        if matches!(
            a.claude_api.reasoning_effort,
            ReasoningEffort::None | ReasoningEffort::Minimal
        ) {
            v.fail("agent.claude_api.reasoning_effort must be default, low, medium, high, xhigh, or max");
        }
        let rt = &a.realtime;
        v.model_id("agent.realtime.model", &rt.model, false);
        v.model_id("agent.realtime.voice", &rt.voice, false);
        v.env_name("agent.realtime.key_env", &rt.key_env);
        match url_parts(&rt.url) {
            Some(("wss", _)) => {}
            _ => v.fail("agent.realtime.url must be a wss:// URL without credentials"),
        }
        v.finite_range(
            "agent.realtime.connect_timeout_seconds",
            rt.connect_timeout_seconds,
            0.0,
            120.0,
            false,
        );
        v.finite_range(
            "agent.realtime.idle_timeout_seconds",
            rt.idle_timeout_seconds,
            0.0,
            600.0,
            false,
        );
    }

    fn check_tts(&self, v: &mut Validator) {
        let t = &self.tts;
        v.finite_range("tts.timeout_seconds", t.timeout_seconds, 0.0, 120.0, false);
        v.executable("tts.piper.executable", &t.piper.executable);
        if t.piper.model.extension().is_none_or(|ext| ext != "onnx") {
            v.fail("tts.piper.model must be a .onnx voice file");
        }
        v.model_id("tts.grok.voice", &t.grok.voice, false);
        if t.grok.language.is_empty()
            || t.grok.language.len() > 16
            || !t
                .grok
                .language
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-')
        {
            v.fail("tts.grok.language must be a language code such as en, or auto");
        }
        v.finite_range("tts.grok.speed", t.grok.speed, 0.7, 1.5, true);
        v.env_name("tts.grok.key_env", &t.grok.key_env);
        v.http_url("tts.hermes.url", &t.hermes.url);
        v.env_name("tts.hermes.token_env", &t.hermes.token_env);
    }

    fn check_messaging(&self, v: &mut Validator) {
        for (service, contact) in self.messaging.enabled() {
            let key = match service {
                Service::WhatsApp => "messaging.whatsapp",
                Service::Signal => "messaging.signal",
            };
            if contact.to.is_empty()
                || contact
                    .to
                    .chars()
                    .any(|c| c.is_whitespace() || c.is_control())
            {
                v.fail(format!("{key}.to must be the contact's number or ID"));
            }
            v.finite_range(
                &format!("{key}.listening.follow_up_seconds"),
                contact.listening.follow_up_seconds,
                0.0,
                600.0,
                false,
            );
            if service == Service::WhatsApp {
                if !contact.account.is_empty() {
                    v.fail("messaging.whatsapp.account applies only to Signal");
                }
                if contact.attachments_dir.is_some() {
                    v.fail("messaging.whatsapp.attachments_dir applies only to Signal");
                }
            } else {
                if !contact.account.is_empty()
                    && !(contact.account.starts_with('+')
                        && contact.account.len() > 1
                        && contact.account[1..].chars().all(|c| c.is_ascii_digit()))
                {
                    v.fail("messaging.signal.account must be a number in +countrycode format");
                }
                if let Some(dir) = &contact.attachments_dir {
                    if !crate::paths::expand_home(dir).is_absolute() {
                        v.fail("messaging.signal.attachments_dir must be an absolute path or start with ~/");
                    }
                }
            }
        }
        if self.messaging.operator_mode && !self.messaging.any_enabled() {
            v.fail("messaging.operator_mode needs at least one messaging contact");
        }
    }

    /// Wake, sleep, shutdown, and spoken phrases must be well-formed and must
    /// never shadow each other.
    fn check_phrases(&self, v: &mut Validator) {
        let mut wakes: Vec<(String, Vec<&str>)> = vec![(
            "wake".into(),
            std::iter::once(self.wake.name.as_str())
                .chain(self.wake.aliases.iter().map(String::as_str))
                .collect(),
        )];
        for (service, contact) in self.messaging.enabled() {
            let key = format!("messaging.{}", service_key(service));
            wakes.push((
                key,
                std::iter::once(contact.wake.as_str())
                    .chain(contact.aliases.iter().map(String::as_str))
                    .collect(),
            ));
        }
        // Spoken phrases are free text; they only need words.
        let mut spoken: Vec<(String, &str)> =
            vec![("wake.confirmation".into(), &self.wake.confirmation)];
        if let Some(sleep) = &self.sleep {
            spoken.push(("sleep.confirmation".into(), &sleep.confirmation));
        }
        if self.shutdown.enabled {
            spoken.push(("shutdown.armed_reply".into(), &self.shutdown.armed_reply));
            spoken.push((
                "shutdown.confirmed_reply".into(),
                &self.shutdown.confirmed_reply,
            ));
        }
        for (service, contact) in self.messaging.enabled() {
            spoken.push((
                format!("messaging.{}.empty_queue_reply", service_key(service)),
                &contact.empty_queue_reply,
            ));
        }

        let mut controls: Vec<(String, Vec<&str>)> = Vec::new();
        if let Some(sleep) = &self.sleep {
            if sleep.phrase.is_empty() && !sleep.aliases.is_empty() {
                v.fail("sleep.phrase is required when sleep.aliases are set");
            }
            if !sleep.phrase.is_empty() {
                controls.push(("sleep".into(), self.sleep_phrases()));
            }
        }
        let s = &self.shutdown;
        if s.enabled {
            for (field, value) in [
                ("shutdown.phrase", &s.phrase),
                ("shutdown.code", &s.code),
                ("shutdown.armed_reply", &s.armed_reply),
                ("shutdown.confirmed_reply", &s.confirmed_reply),
            ] {
                if value.is_empty() {
                    v.fail(format!("{field} is required when shutdown is enabled"));
                }
            }
            controls.push((
                "shutdown.phrase".into(),
                std::iter::once(s.phrase.as_str())
                    .chain(s.phrase_aliases.iter().map(String::as_str))
                    .collect(),
            ));
            controls.push((
                "shutdown.code".into(),
                std::iter::once(s.code.as_str())
                    .chain(s.code_aliases.iter().map(String::as_str))
                    .collect(),
            ));
            v.finite_range(
                "shutdown.confirm_window_seconds",
                s.confirm_window_seconds,
                0.0,
                300.0,
                false,
            );
            if !s.armed_reply.is_empty()
                && phrases::normalize(&s.armed_reply) == phrases::normalize(&s.confirmed_reply)
            {
                v.fail("shutdown.armed_reply and shutdown.confirmed_reply must differ");
            }
        }

        // Every phrase must contain words and stay short.
        for (field, list) in wakes.iter().chain(controls.iter()) {
            for phrase in list {
                if !phrase_ok(phrase) {
                    v.fail(format!(
                        "{field} entries must be words, at most 200 characters"
                    ));
                }
            }
        }
        for (field, phrase) in &spoken {
            if !phrase.is_empty() && !phrase_ok(phrase) {
                v.fail(format!(
                    "{field} must be empty or words, at most 200 characters"
                ));
            }
        }
        if v.has_failures() {
            return;
        }

        // No two groups may share a phrase.
        let mut owners: BTreeMap<String, String> = BTreeMap::new();
        for (field, list) in wakes.iter().chain(controls.iter()) {
            for phrase in list {
                let key = phrases::normalize(phrase);
                match owners.get(&key) {
                    Some(owner) if owner != field => {
                        v.fail(format!("\"{phrase}\" is used by both {owner} and {field}"));
                    }
                    _ => {
                        owners.insert(key, field.clone());
                    }
                }
            }
        }
        // Spoken replies must not sound like a control or wake phrase.
        for (field, phrase) in &spoken {
            if let Some(owner) = owners.get(&phrases::normalize(phrase)) {
                v.fail(format!("{field} must differ from the {owner} phrases"));
            }
        }
        // A longer wake name must not swallow the words of a control spoken
        // after a shorter one, e.g. wake "charlotte go" vs. "charlotte, go to sleep".
        let all_wakes: Vec<Phrase> = wakes
            .iter()
            .flat_map(|(_, list)| list.iter().map(|p| Phrase::new(p)))
            .collect();
        for (field, list) in &controls {
            for control in list {
                for wake in &all_wakes {
                    let utterance = format!("{} {}", wake.text(), control);
                    let (_, consumed) = phrases::longest_prefix(&utterance, &all_wakes)
                        .expect("the wake itself always matches");
                    if consumed > wake.len() {
                        v.fail(format!(
                            "a wake phrase would swallow part of {field} \"{control}\" when said after \"{}\"",
                            wake.text()
                        ));
                    }
                }
            }
        }
    }
}

fn phrase_ok(phrase: &str) -> bool {
    !phrases::normalize(phrase).is_empty()
        && phrase.chars().count() <= 200
        && !phrase.chars().any(char::is_control)
}

pub fn service_key(service: Service) -> &'static str {
    match service {
        Service::WhatsApp => "whatsapp",
        Service::Signal => "signal",
    }
}

fn is_router_device(name: &str) -> bool {
    let lower = name.to_ascii_lowercase();
    [
        "default",
        "sysdefault",
        "pulse",
        "pipewire",
        "jack",
        "dmix",
        "dsnoop",
    ]
    .iter()
    .any(|router| lower == *router || lower.starts_with(&format!("{router}:")))
}

/// Split a URL into scheme and host, rejecting credentials, queries, and fragments.
fn url_parts(url: &str) -> Option<(&str, &str)> {
    if url.chars().any(|c| c.is_whitespace() || c.is_control()) {
        return None;
    }
    let (scheme, rest) = url.split_once("://")?;
    let authority = rest.split('/').next()?;
    if authority.is_empty() || authority.contains('@') || rest.contains('?') || rest.contains('#') {
        return None;
    }
    Some((scheme, authority))
}

#[derive(Default)]
struct Validator {
    problems: Vec<String>,
}

impl Validator {
    fn fail(&mut self, problem: impl Into<String>) {
        self.problems.push(problem.into());
    }

    fn has_failures(&self) -> bool {
        !self.problems.is_empty()
    }

    fn finish(self) -> Result<(), ConfigError> {
        if self.problems.is_empty() {
            Ok(())
        } else {
            Err(ConfigError {
                problems: self.problems,
            })
        }
    }

    /// `low < value <= high`, or `low <= value <= high` when `inclusive_low`.
    fn finite_range(&mut self, field: &str, value: f64, low: f64, high: f64, inclusive_low: bool) {
        let above = if inclusive_low {
            value >= low
        } else {
            value > low
        };
        if !value.is_finite() || !above || value > high {
            let bound = if inclusive_low {
                "from"
            } else {
                "greater than"
            };
            self.fail(format!("{field} must be {bound} {low} and at most {high}"));
        }
    }

    fn env_name(&mut self, field: &str, name: &str) {
        let mut chars = name.chars();
        let valid = chars
            .next()
            .is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
            && chars.all(|c| c.is_ascii_alphanumeric() || c == '_');
        if !valid {
            self.fail(format!(
                "{field} must name an environment variable, not hold a secret"
            ));
        }
    }

    fn executable(&mut self, field: &str, value: &str) {
        let path = Path::new(value);
        let valid = !value.is_empty()
            && !value.chars().any(|c| c.is_whitespace() || c.is_control())
            && (path.is_absolute() || !value.contains('/'));
        if !valid {
            self.fail(format!(
                "{field} must be an executable name or absolute path, without arguments"
            ));
        }
    }

    fn model_id(&mut self, field: &str, value: &str, allow_empty: bool) {
        if value.is_empty() {
            if !allow_empty {
                self.fail(format!("{field} is required"));
            }
            return;
        }
        let valid = value.len() <= 128
            && value
                .chars()
                .next()
                .is_some_and(|c| c.is_ascii_alphanumeric())
            && value
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || "._:-".contains(c));
        if !valid {
            self.fail(format!(
                "{field} must be an identifier such as a model or voice name"
            ));
        }
    }

    fn http_url(&mut self, field: &str, url: &str) {
        if !matches!(url_parts(url), Some(("http" | "https", _))) {
            self.fail(format!(
                "{field} must be an http(s) base URL without credentials or query"
            ));
        }
    }
}

#[cfg(test)]
pub mod tests_support {
    pub const MINIMAL: &str = r#"
        [audio]
        input = "plughw:CARD=AllInOneCable,DEV=0"
        output = "plughw:CARD=AllInOneCable,DEV=0"
        [ptt]
        port = "/dev/serial/by-id/usb-AIOC"
        [wake]
        name = "charlotte"
        aliases = ["charlot"]
    "#;
}

#[cfg(test)]
mod tests {
    use super::tests_support::MINIMAL;
    use super::*;

    fn parse(extra: &str) -> Result<Config, ConfigError> {
        Config::parse(
            &format!("{MINIMAL}\n{extra}"),
            PathBuf::from("/etc/walkietalk"),
        )
    }

    fn problems(extra: &str) -> String {
        parse(extra).unwrap_err().problems.join("\n")
    }

    #[test]
    fn minimal_config_uses_defaults() {
        let config = parse("").unwrap();
        assert_eq!(config.agent.backend, AgentBackend::Stub);
        assert_eq!(config.stt.backend, SttBackend::Whisper);
        assert_eq!(config.tts.backend, TtsBackend::Piper);
        assert_eq!(config.ptt.line, PttLine::Dtr);
        assert_eq!(config.listening.mode, ListeningMode::Conversation);
        assert!(!config.shutdown.enabled);
        assert!(!config.messaging.any_enabled());
    }

    #[test]
    fn template_is_valid() {
        let config = Config::parse(crate::setup::CONFIG_TEMPLATE, PathBuf::from("/tmp")).unwrap();
        assert!(config.sleep.is_some());
    }

    #[test]
    fn unknown_fields_are_rejected() {
        let text = MINIMAL.replace("[wake]", "[wake]\nnmae = \"x\"");
        let err = Config::parse(&text, PathBuf::new()).unwrap_err();
        assert!(err.to_string().contains("nmae"), "{err}");
    }

    #[test]
    fn missing_required_section_is_rejected() {
        let text = MINIMAL.replace("[ptt]\n        port = \"/dev/serial/by-id/usb-AIOC\"", "");
        assert!(Config::parse(&text, PathBuf::new()).is_err());
    }

    #[test]
    fn all_problems_are_reported_together() {
        let p = problems("[radio]\nmax_tx_seconds = 0\n[vad]\nhangover_ms = 0\n");
        assert!(p.contains("radio.max_tx_seconds"));
        assert!(p.contains("vad.hangover_ms"));
    }

    #[test]
    fn settle_must_be_shorter_than_transmit_cap() {
        assert!(problems("[radio]\nmax_tx_seconds = 1\nsettle_seconds = 1.5\n").contains("settle"));
    }

    #[test]
    fn router_devices_are_rejected() {
        let text = MINIMAL.replace(
            "input = \"plughw:CARD=AllInOneCable,DEV=0\"",
            "input = \"default\"",
        );
        assert!(Config::parse(&text, PathBuf::new()).is_err());
    }

    #[test]
    fn secrets_cannot_be_placed_in_env_name_fields() {
        assert!(problems("[agent.claude_api]\nkey_env = \"sk-ant-123 456\"\n").contains("key_env"));
    }

    #[test]
    fn executables_must_not_contain_arguments() {
        assert!(problems("[agent.codex]\nexecutable = \"codex --yolo\"\n").contains("executable"));
        assert!(problems("[agent.codex]\nexecutable = \"bin/codex\"\n").contains("executable"));
        assert!(parse("[agent.codex]\nexecutable = \"/opt/codex/bin/codex\"\n").is_ok());
    }

    #[test]
    fn selected_backend_requires_its_model() {
        assert!(problems("[agent]\nbackend = \"claude-api\"\n").contains("claude_api.model"));
        assert!(parse("[agent]\nbackend = \"claude-api\"\n[agent.claude_api]\nmodel = \"claude-sonnet-5\"\n").is_ok());
    }

    #[test]
    fn morse_callsign_must_be_alphanumeric() {
        let p = problems("[radio.station_id]\ncallsign = \"WSOF-426\"\nmethod = \"morse\"\n");
        assert!(p.contains("morse"));
        assert!(
            parse("[radio.station_id]\ncallsign = \"WSOF426 1\"\nmethod = \"morse\"\n").is_ok()
        );
    }

    #[test]
    fn unknown_instruction_placeholders_are_rejected() {
        assert!(
            problems("[agent]\ninstructions = \"Use {max_reply_chars} and {mood}\"\n")
                .contains("mood")
        );
        assert!(parse("[agent]\ninstructions = \"Braces {{ok}} and {max_words}\"\n").is_ok());
    }

    #[test]
    fn sleep_phrase_cannot_equal_wake_name() {
        let p = problems("[sleep]\nphrase = \"Charlot!\"\n");
        assert!(p.contains("charlot") || p.contains("Charlot"), "{p}");
    }

    #[test]
    fn shutdown_requires_its_phrases_when_enabled() {
        let p = problems("[shutdown]\nenabled = true\n");
        assert!(p.contains("shutdown.phrase"));
        assert!(p.contains("shutdown.code"));
    }

    #[test]
    fn shutdown_phrase_and_code_must_differ() {
        let p = problems(
            "[shutdown]\nenabled = true\nphrase = \"bird\"\ncode = \"Bird\"\narmed_reply = \"armed\"\nconfirmed_reply = \"bye\"\n",
        );
        assert!(p.contains("used by both"), "{p}");
    }

    #[test]
    fn messaging_wake_cannot_collide_with_agent_wake() {
        let p = problems("[messaging.signal]\nwake = \"charlotte\"\nto = \"+15551234567\"\n");
        assert!(p.contains("used by both"), "{p}");
    }

    #[test]
    fn longer_wake_cannot_swallow_a_control_phrase() {
        let p = problems(
            "[sleep]\nphrase = \"go to sleep\"\n[messaging.whatsapp]\nwake = \"charlotte go\"\nto = \"+15551234567\"\n",
        );
        assert!(p.contains("swallow"), "{p}");
    }

    #[test]
    fn spoken_reply_cannot_match_a_control_phrase() {
        let p = problems("[sleep]\nphrase = \"go to sleep\"\nconfirmation = \"Go to sleep.\"\n");
        assert!(p.contains("sleep.confirmation"), "{p}");
    }

    #[test]
    fn operator_mode_needs_a_contact() {
        assert!(problems("[messaging]\noperator_mode = true\n").contains("operator_mode"));
    }

    #[test]
    fn piper_model_resolves_relative_to_config() {
        let config = parse("[tts.piper]\nmodel = \"voices/amy.onnx\"\n").unwrap();
        assert_eq!(
            config.piper_model(),
            PathBuf::from("/etc/walkietalk/voices/amy.onnx")
        );
    }
}
