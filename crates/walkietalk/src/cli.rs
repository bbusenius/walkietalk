//! Command-line interface.

use std::path::{Path, PathBuf};
use std::process::ExitCode;

use anyhow::{Context, bail};
use clap::{Args, Parser, Subcommand};

use crate::config::Config;
use crate::credentials::{self, Credentials};
use crate::commands::{hardware, speech};
use crate::radio::TransmitConsent;
use crate::{paths, setup, signals, talk, ui};

#[derive(Parser)]
#[command(
    name = "walkietalk",
    version,
    about = "A Linux radio bridge for AI agents and messaging contacts"
)]
struct Cli {
    #[command(flatten)]
    global: Global,
    #[command(subcommand)]
    command: Command,
}

#[derive(Args)]
struct Global {
    /// Settings file [default: ~/.config/walkietalk/config.toml]
    #[arg(short, long, global = true, value_name = "FILE")]
    config: Option<PathBuf>,
    /// Credentials file to use instead of the one beside the config
    #[arg(
        long,
        global = true,
        value_name = "FILE",
        conflicts_with = "no_credentials"
    )]
    credentials: Option<PathBuf>,
    /// Use only exported environment variables for secrets
    #[arg(long, global = true)]
    no_credentials: bool,
}

#[derive(Subcommand)]
enum Command {
    /// Create a new private settings directory
    Init {
        /// Directory to create [default: ~/.config/walkietalk]
        #[arg(long, value_name = "DIR")]
        directory: Option<PathBuf>,
    },
    /// Validate the settings without touching hardware, network, or logins
    ConfigCheck,
    /// Check that the configured devices and backends are ready (never transmits)
    Check,
    /// List audio devices and serial ports for the config
    Devices {
        /// Show every ALSA device, not just sound cards by name
        #[arg(long)]
        all: bool,
    },
    /// Key the transmitter briefly (simulated unless --transmit)
    Ptt {
        #[arg(long, default_value_t = 1.0, value_name = "N")]
        seconds: f64,
        /// Really key the radio (requires --config)
        #[arg(long)]
        transmit: bool,
    },
    /// Ask the text agent one question (no audio or PTT)
    AgentCheck {
        #[arg(default_value = "Hello. Please introduce yourself in one sentence.")]
        text: String,
    },
    /// Synthesize speech to a new WAV file (no hardware)
    TtsCheck {
        text: String,
        #[arg(long, value_name = "FILE")]
        output: PathBuf,
    },
    /// One realtime speech-to-speech turn saved to a new WAV (grok-realtime)
    VoiceAgentCheck {
        #[command(flatten)]
        input: Input,
        #[arg(long, value_name = "FILE")]
        output: PathBuf,
        /// Run the transmit logic too (simulated unless --transmit)
        #[arg(long)]
        supervised: bool,
        /// With --supervised: really key the radio (requires --config)
        #[arg(long, requires = "supervised")]
        transmit: bool,
    },
    /// Download the local Whisper model
    Models,
    /// Transcribe one utterance from a WAV or the radio (never transmits)
    Listen {
        #[command(flatten)]
        input: Input,
        /// Seconds to wait for speech with --capture
        #[arg(long, default_value_t = 60.0, value_name = "N")]
        timeout: f64,
    },
    /// Listen, wake-gate, and answer (speaks on the radio only with --transmit)
    Talk {
        #[command(flatten)]
        input: Input,
        /// Speak replies on the radio (requires --config)
        #[arg(long)]
        transmit: bool,
        /// Handle one utterance, then exit
        #[arg(long)]
        once: bool,
        /// With --capture --once: seconds to wait for speech
        #[arg(long, value_name = "N")]
        timeout: Option<f64>,
    },
    /// Play a WAV over the radio (simulated unless --transmit)
    Play {
        wav: PathBuf,
        /// Really transmit (requires --config)
        #[arg(long)]
        transmit: bool,
    },
}

/// Audio input: a WAV file or live capture.
#[derive(Args)]
#[group(required = true, multiple = false)]
struct Input {
    /// Read the utterance from this WAV file
    wav: Option<PathBuf>,
    /// Listen on the configured capture device
    #[arg(long)]
    capture: bool,
}

/// Where the config came from. Only an explicitly named file may key the radio.
#[derive(Debug, Clone)]
pub enum ConfigPath {
    Explicit(PathBuf),
    Default(PathBuf),
}

impl ConfigPath {
    pub fn path(&self) -> &Path {
        match self {
            ConfigPath::Explicit(p) | ConfigPath::Default(p) => p,
        }
    }

    pub fn is_explicit(&self) -> bool {
        matches!(self, ConfigPath::Explicit(_))
    }
}

impl Global {
    fn config_path(&self) -> ConfigPath {
        match &self.config {
            Some(path) => ConfigPath::Explicit(path.clone()),
            None => ConfigPath::Default(paths::default_config_file()),
        }
    }

    fn load_config(&self) -> anyhow::Result<(ConfigPath, Config)> {
        let source = self.config_path();
        if let ConfigPath::Default(path) = &source {
            if !path.exists() {
                bail!(
                    "no settings at {}; run `walkietalk init` or pass --config FILE",
                    path.display()
                );
            }
        }
        let config = Config::load(source.path())?;
        Ok((source, config))
    }

    fn load_credentials(&self, config: &ConfigPath) -> anyhow::Result<Credentials> {
        if self.no_credentials {
            return Ok(Credentials::environment_only());
        }
        if let Some(path) = &self.credentials {
            return Credentials::load(&paths::expand_home(path), true);
        }
        let beside = config
            .path()
            .canonicalize()
            .ok()
            .and_then(|p| p.parent().map(|d| d.join(credentials::FILE_NAME)));
        match beside {
            Some(path) => Credentials::load(&path, false),
            None => Ok(Credentials::environment_only()),
        }
    }
}

pub fn main() -> ExitCode {
    let cli = Cli::parse();
    if let Err(err) = signals::install() {
        ui::error!("Error: cannot install signal handlers: {err:#}");
        return ExitCode::FAILURE;
    }
    let runtime = match tokio::runtime::Runtime::new() {
        Ok(runtime) => runtime,
        Err(err) => {
            ui::error!("Error: cannot start the async runtime: {err}");
            return ExitCode::FAILURE;
        }
    };
    match runtime.block_on(run(cli)) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            ui::error!("Error: {err:#}");
            ExitCode::FAILURE
        }
    }
}

async fn run(cli: Cli) -> anyhow::Result<()> {
    let global = &cli.global;
    match cli.command {
        Command::Init { directory } => {
            let dir = directory
                .map(|d| paths::expand_home(&d))
                .unwrap_or_else(paths::default_settings_dir);
            setup::init(&dir)?;
            ui::status!("Created {}", dir.join("config.toml").display());
            ui::status!(
                "Created {} (private)",
                dir.join(credentials::FILE_NAME).display()
            );
            ui::status!("Edit the device names and backends before using hardware.");
            Ok(())
        }
        Command::ConfigCheck => {
            let (source, config) = global.load_config()?;
            let creds = global.load_credentials(&source).context("credentials")?;
            ui::status!("Config OK: {}", source.path().display());
            print_summary(&config, &creds);
            ui::status!("No hardware, network, or login was checked.");
            Ok(())
        }
        Command::Check => {
            let (source, config) = global.load_config()?;
            let creds = global.load_credentials(&source)?;
            blocking(move || crate::commands::check::run(&config, &creds)).await
        }
        Command::Devices { all } => blocking(move || hardware::devices(all)).await,
        Command::Ptt { seconds, transmit } => {
            let (source, config) = global.load_config()?;
            let consent = TransmitConsent::grant(transmit, &source)?;
            blocking(move || hardware::ptt(&config, consent, seconds)).await
        }
        Command::AgentCheck { text } => {
            let (source, config) = global.load_config()?;
            let creds = global.load_credentials(&source)?;
            speech::agent_check(&config, &creds, &text).await
        }
        Command::TtsCheck { text, output } => {
            let (source, config) = global.load_config()?;
            let creds = global.load_credentials(&source)?;
            speech::tts_check(&config, &creds, &text, &output).await
        }
        Command::VoiceAgentCheck { input, output, supervised, transmit } => {
            let (source, config) = global.load_config()?;
            let creds = global.load_credentials(&source)?;
            let consent = TransmitConsent::grant(transmit, &source)?;
            speech::voice_agent_check(&config, &creds, input.wav, &output, supervised, consent).await
        }
        Command::Models => {
            let (_, config) = global.load_config()?;
            speech::models(&config).await
        }
        Command::Listen { input, timeout } => {
            let (source, config) = global.load_config()?;
            let creds = global.load_credentials(&source)?;
            speech::listen(&config, &creds, input.wav, timeout).await
        }
        Command::Talk { input, transmit, once, timeout } => {
            let (source, config) = global.load_config()?;
            let creds = global.load_credentials(&source)?;
            let consent = TransmitConsent::grant(transmit, &source)?;
            let options = talk::Options { wav: input.wav, consent, once, timeout };
            talk::run(config, creds, options).await
        }
        Command::Play { wav, transmit } => {
            let (source, config) = global.load_config()?;
            let consent = TransmitConsent::grant(transmit, &source)?;
            blocking(move || hardware::play(&config, consent, &wav)).await
        }
    }
}

/// Run blocking work off the async threads.
async fn blocking<T: Send + 'static>(f: impl FnOnce() -> anyhow::Result<T> + Send + 'static) -> anyhow::Result<T> {
    tokio::task::spawn_blocking(f).await.context("worker thread failed")?
}

fn print_summary(config: &Config, creds: &Credentials) {
    let realtime = config.agent.backend.is_realtime();
    if realtime {
        ui::status!("Agent: grok-realtime (speech-to-speech; replaces STT, agent, and voice)");
    } else {
        ui::status!("Speech recognition: {}", config.stt.backend);
        ui::status!("Agent: {}", config.agent.backend);
        ui::status!("Voice: {}", config.tts.backend);
    }
    ui::status!(
        "Listening: {}, follow-up window {}s",
        config.listening.mode,
        config.listening.follow_up_seconds
    );
    for (service, contact) in config.messaging.enabled() {
        ui::status!("Messaging: {service} via wake \"{}\"", contact.wake);
    }
    if config.messaging.operator_mode {
        ui::status!("Operator mode: every message waits for local review");
    }
    if let Some(path) = creds.path() {
        ui::status!("Credentials file: {}", path.display());
    }
}
