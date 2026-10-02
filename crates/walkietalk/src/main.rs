//! walkietalk: a Linux radio bridge for AI agents and messaging contacts.

mod agent;
mod audio;
mod backends;
mod cli;
mod commands;
mod config;
mod credentials;
mod exec;
mod gate;
mod grok_login;
mod http;
mod messaging;
mod operator;
mod panel;
mod paths;
mod phrases;
mod radio;
mod realtime;
mod setup;
mod signals;
mod stt;
mod talk;
mod tts;
mod sys;
mod ui;
mod xai;

fn main() -> std::process::ExitCode {
    cli::main()
}
