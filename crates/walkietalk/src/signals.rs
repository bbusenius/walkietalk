//! Ctrl+C, SIGTERM, and panics.
//!
//! The first stop signal releases every registered transmitter at once,
//! then asks the program to wind down. A second signal exits immediately
//! (after releasing again). A panic also releases the transmitter.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};

use signal_hook::consts::{SIGINT, SIGTERM};
use signal_hook::iterator::Signals;
use tokio_util::sync::CancellationToken;

use crate::radio::ptt::Ptt;

struct State {
    ptts: Mutex<Vec<Ptt>>,
    token: CancellationToken,
    stopping: AtomicBool,
}

fn state() -> &'static State {
    static STATE: OnceLock<State> = OnceLock::new();
    STATE.get_or_init(|| State {
        ptts: Mutex::new(Vec::new()),
        token: CancellationToken::new(),
        stopping: AtomicBool::new(false),
    })
}

fn release_all() {
    for ptt in state().ptts.lock().unwrap_or_else(|e| e.into_inner()).iter() {
        ptt.stop();
    }
}

/// Install the handlers. Call once at startup.
pub fn install() -> anyhow::Result<()> {
    let mut signals = Signals::new([SIGINT, SIGTERM])?;
    std::thread::Builder::new().name("signals".into()).spawn(move || {
        for _ in signals.forever() {
            release_all();
            if state().stopping.swap(true, Ordering::SeqCst) {
                std::process::exit(130);
            }
            crate::ui::warning!("Stopping; press Ctrl+C again to exit immediately.");
            state().token.cancel();
        }
    })?;
    let default = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        release_all();
        default(info);
    }));
    Ok(())
}

/// Release this transmitter on any stop signal or panic.
pub fn protect(ptt: Ptt) {
    state().ptts.lock().unwrap_or_else(|e| e.into_inner()).push(ptt);
}

pub fn stop_requested() -> bool {
    state().stopping.load(Ordering::SeqCst)
}

/// Cancelled when a stop signal arrives.
pub fn token() -> CancellationToken {
    state().token.clone()
}
