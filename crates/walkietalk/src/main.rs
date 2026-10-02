//! walkietalk: a Linux radio bridge for AI agents and messaging contacts.

mod agent;
mod audio;
mod backends;
mod cli;
mod commands;
mod config;
mod credentials;
mod exec;
mod paths;
mod phrases;
mod radio;
mod setup;
mod signals;
mod stt;
mod tts;
mod sys;
mod ui;

fn main() -> std::process::ExitCode {
    cli::main()
}
