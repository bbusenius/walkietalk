//! Command-line interface.

use std::path::{Path, PathBuf};
use std::process::ExitCode;

use anyhow::{Context, bail};
use clap::{Args, Parser, Subcommand};

use crate::commands::{hardware, speech};
use crate::config::Config;
use crate::credentials::{self, Credentials};
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
        /// Show the operator panel (operator mode only)
        #[arg(long)]
        panel: bool,
    },
    /// Control a running operator-mode talk from another terminal
    Operator {
        #[arg(value_enum, default_value = "status")]
        action: crate::operator::Action,
        /// Act on the oldest approved incoming message instead of the review head
        #[arg(long)]
        approved: bool,
        /// New words for `edit` (otherwise an editor opens)
        #[arg(long, value_name = "T")]
        text: Option<String>,
        /// Seconds to wait for the command to finish
        #[arg(long, default_value_t = 120.0, value_name = "N")]
        timeout: f64,
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

impl Global {
    fn load_config(&self) -> anyhow::Result<(PathBuf, Config)> {
        let path = self
            .config
            .clone()
            .unwrap_or_else(paths::default_config_file);
        if self.config.is_none() && !path.exists() {
            bail!(
                "no settings at {}; run `walkietalk init` or pass --config FILE",
                path.display()
            );
        }
        let config = Config::load(&path)?;
        Ok((path, config))
    }

    fn load_credentials(&self, config: &Path) -> anyhow::Result<Credentials> {
        if self.no_credentials {
            return Ok(Credentials::environment_only());
        }
        if let Some(path) = &self.credentials {
            return Credentials::load(&paths::expand_home(path), true);
        }
        let beside = config
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
    // `talk` winds down by itself on a stop request; anything else stops at
    // once (dropping its work kills any helper programs).
    let talk = matches!(cli.command, Command::Talk { .. });
    let result = runtime.block_on(async move {
        if talk {
            return run(cli).await;
        }
        let stop = signals::token();
        tokio::select! {
            result = run(cli) => result,
            _ = stop.cancelled() => Err(anyhow::anyhow!("stopped")),
        }
    });
    // A worker stuck in a driver call must not keep the process alive.
    runtime.shutdown_timeout(std::time::Duration::from_secs(2));
    match result {
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
            let (path, config) = global.load_config()?;
            let creds = global.load_credentials(&path).context("credentials")?;
            ui::status!("Config OK: {}", path.display());
            print_summary(&config, &creds);
            ui::status!("No hardware, network, or login was checked.");
            Ok(())
        }
        Command::Check => {
            let (path, config) = global.load_config()?;
            let creds = global.load_credentials(&path)?;
            blocking(move || crate::commands::check::run(&config, &creds)).await
        }
        Command::Devices { all } => blocking(move || hardware::devices(all)).await,
        Command::Ptt { seconds, transmit } => {
            let (path, config) = global.load_config()?;
            let consent = consent(transmit, &path, &config);
            blocking(move || hardware::ptt(&config, consent, seconds)).await
        }
        Command::AgentCheck { text } => {
            let (path, config) = global.load_config()?;
            let creds = global.load_credentials(&path)?;
            speech::agent_check(&config, &creds, &text).await
        }
        Command::TtsCheck { text, output } => {
            let (path, config) = global.load_config()?;
            let creds = global.load_credentials(&path)?;
            speech::tts_check(&config, &creds, &text, &output).await
        }
        Command::VoiceAgentCheck {
            input,
            output,
            supervised,
            transmit,
        } => {
            let (path, config) = global.load_config()?;
            let creds = global.load_credentials(&path)?;
            let consent = consent(transmit, &path, &config);
            speech::voice_agent_check(&config, &creds, input.wav, &output, supervised, consent)
                .await
        }
        Command::Models => {
            let (_, config) = global.load_config()?;
            speech::models(&config).await
        }
        Command::Listen { input, timeout } => {
            let (path, config) = global.load_config()?;
            let creds = global.load_credentials(&path)?;
            speech::listen(&config, &creds, input.wav, timeout).await
        }
        Command::Talk {
            input,
            transmit,
            once,
            timeout,
            panel,
        } => {
            let (path, config) = global.load_config()?;
            let creds = global.load_credentials(&path)?;
            let consent = consent(transmit, &path, &config);
            let options = talk::Options {
                wav: input.wav,
                consent,
                once,
                timeout,
                config_path: path,
                panel,
            };
            talk::run(config, creds, options).await
        }
        Command::Operator {
            action,
            approved,
            text,
            timeout,
        } => operator(global, action, approved, text, timeout).await,
        Command::Play { wav, transmit } => {
            let (path, config) = global.load_config()?;
            let consent = consent(transmit, &path, &config);
            blocking(move || hardware::play(&config, consent, &wav)).await
        }
    }
}

async fn operator(
    global: &Global,
    action: crate::operator::Action,
    approved: bool,
    text: Option<String>,
    timeout: f64,
) -> anyhow::Result<()> {
    use crate::operator::{Action, client};
    anyhow::ensure!(
        timeout > 0.0 && timeout <= 600.0,
        "--timeout must be greater than 0 and at most 600"
    );
    anyhow::ensure!(
        text.is_none() || action == Action::Edit,
        "--text applies only to edit"
    );
    anyhow::ensure!(
        !(approved && action == Action::Edit),
        "approved messages can't be edited; deny to drop it"
    );
    let config = global.config.as_deref().map(paths::expand_home);
    let config = config.as_deref();
    let timeout = std::time::Duration::from_secs_f64(timeout);
    let mut bound = None;
    let text = match (action, text) {
        (Action::Edit, None) => {
            let (item, revision) = client::current_text(config).await?;
            // The edit applies only to the item shown in the editor.
            bound = Some(revision);
            let current = item.content.clone();
            match tokio::task::spawn_blocking(move || client::prompt_edit(&current)).await?? {
                Some(text) => Some(text),
                None => {
                    ui::status!("Edit cancelled.");
                    return Ok(());
                }
            }
        }
        (_, text) => text,
    };
    if !client::request(config, action, approved, text, bound, timeout).await? {
        bail!("the operator command did not complete; see above");
    }
    Ok(())
}

/// Grant consent for `--transmit`, naming the settings and port that will key the radio.
fn consent(transmit: bool, path: &Path, config: &Config) -> Option<TransmitConsent> {
    let consent = TransmitConsent::grant(transmit);
    if consent.is_some() {
        ui::status!(
            "Transmitting with {}, PTT on {}",
            path.display(),
            config.ptt.port.display()
        );
    }
    consent
}

/// Run blocking work off the async threads.
async fn blocking<T: Send + 'static>(
    f: impl FnOnce() -> anyhow::Result<T> + Send + 'static,
) -> anyhow::Result<T> {
    tokio::task::spawn_blocking(f)
        .await
        .context("worker thread failed")?
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
