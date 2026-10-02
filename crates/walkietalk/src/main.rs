//! walkietalk: a Linux radio bridge for AI agents and messaging contacts.

mod agent;
mod cli;
mod config;
mod credentials;
mod paths;
mod phrases;
mod setup;
mod sys;
mod ui;

fn main() -> std::process::ExitCode {
    cli::main()
}
