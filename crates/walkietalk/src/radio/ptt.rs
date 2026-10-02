//! The push-to-talk line and the supervisor thread that owns it.
//!
//! Exactly one thread touches the serial line. Every key request carries an
//! absolute deadline, and the supervisor releases the line when it passes,
//! whatever the rest of the program is doing. A failed key or release is a
//! fault: the supervisor refuses to key again.

use std::os::fd::AsRawFd;
use std::path::Path;
use std::sync::mpsc;
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use anyhow::{Context, bail};
use serialport::SerialPort;

use crate::config::PttLine as Line;
use crate::ui;

/// Hardware that keys the transmitter.
pub trait PttLine: Send {
    /// Assert the keying line (the other line stays low).
    fn key(&mut self) -> anyhow::Result<()>;
    /// Drop both lines and confirm they are low.
    fn release(&mut self) -> anyhow::Result<()>;
}

/// A serial port whose DTR or RTS line keys the radio.
pub struct SerialLine {
    port: serialport::TTYPort,
    line: Line,
    keyed: bool,
}

impl SerialLine {
    /// Open the port exclusively and immediately drop both lines.
    pub fn open(path: &Path, line: Line) -> anyhow::Result<SerialLine> {
        let name = path.to_str().context("serial path must be UTF-8")?;
        let mut port = serialport::new(name, 9600)
            .dtr_on_open(false)
            .timeout(Duration::from_millis(500))
            .open_native()
            .map_err(|err| {
                anyhow::anyhow!(
                    "cannot open PTT port {name}: {err}; check the path and your permissions"
                )
            })?;
        port.set_exclusive(true).map_err(|err| {
            anyhow::anyhow!("PTT port {name} is in use by another program: {err}")
        })?;
        let mut serial = SerialLine {
            port,
            line,
            keyed: false,
        };
        serial
            .release()
            .context("PTT lines could not be set low after opening")?;
        Ok(serial)
    }

    fn modem_bits(&self) -> anyhow::Result<libc::c_int> {
        let mut bits: libc::c_int = 0;
        // SAFETY: TIOCMGET writes one c_int through the pointer, which is valid.
        let rc = unsafe { libc::ioctl(self.port.as_raw_fd(), libc::TIOCMGET, &mut bits) };
        if rc != 0 {
            bail!(
                "cannot read modem lines: {}",
                std::io::Error::last_os_error()
            );
        }
        Ok(bits)
    }

    fn set(&mut self, line: Line, high: bool) -> anyhow::Result<()> {
        let result = match line {
            Line::Dtr => self.port.write_data_terminal_ready(high),
            Line::Rts => self.port.write_request_to_send(high),
        };
        result.map_err(|err| anyhow::anyhow!("cannot set {line}: {err}"))
    }
}

impl PttLine for SerialLine {
    fn key(&mut self) -> anyhow::Result<()> {
        let other = match self.line {
            Line::Dtr => Line::Rts,
            Line::Rts => Line::Dtr,
        };
        self.set(other, false)?;
        self.keyed = true;
        self.set(self.line, true)?;
        ui::event!("PTT on");
        Ok(())
    }

    fn release(&mut self) -> anyhow::Result<()> {
        // Drop the keying line first and attempt both even if one fails.
        let first = self.set(self.line, false);
        let other = match self.line {
            Line::Dtr => Line::Rts,
            Line::Rts => Line::Dtr,
        };
        let second = self.set(other, false);
        first.and(second)?;
        let bits = self.modem_bits()?;
        if bits & (libc::TIOCM_DTR | libc::TIOCM_RTS) != 0 {
            bail!("a keying line is still high after release");
        }
        if std::mem::take(&mut self.keyed) {
            ui::event!("PTT off");
        }
        Ok(())
    }
}

/// Prints what a real line would do.
#[derive(Default)]
pub struct DryLine {
    keyed: bool,
}

impl PttLine for DryLine {
    fn key(&mut self) -> anyhow::Result<()> {
        self.keyed = true;
        ui::event!("DRY RUN: PTT on");
        Ok(())
    }

    fn release(&mut self) -> anyhow::Result<()> {
        if std::mem::take(&mut self.keyed) {
            ui::event!("DRY RUN: PTT off");
        }
        Ok(())
    }
}

enum Command {
    Key {
        deadline: Instant,
        reply: mpsc::Sender<anyhow::Result<()>>,
    },
    Release {
        reply: mpsc::Sender<anyhow::Result<bool>>,
    },
    /// Release now and never key again.
    Stop,
}

/// Handle to the PTT supervisor.
#[derive(Clone)]
pub struct Ptt {
    commands: mpsc::Sender<Command>,
}

/// Owns the supervisor thread; dropping it releases the line and stops it.
pub struct PttOwner {
    ptt: Ptt,
    thread: Option<JoinHandle<()>>,
}

impl PttOwner {
    pub fn start(line: Box<dyn PttLine>) -> PttOwner {
        let (tx, rx) = mpsc::channel();
        let thread = std::thread::Builder::new()
            .name("ptt".into())
            .spawn(move || supervise(line, rx))
            .expect("start PTT supervisor");
        PttOwner {
            ptt: Ptt { commands: tx },
            thread: Some(thread),
        }
    }

    pub fn handle(&self) -> Ptt {
        self.ptt.clone()
    }
}

impl Drop for PttOwner {
    fn drop(&mut self) {
        self.ptt.stop();
        if let Some(thread) = self.thread.take() {
            // Bounded: a stuck driver must not hang shutdown forever.
            let deadline = Instant::now() + Duration::from_secs(2);
            while !thread.is_finished() && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(10));
            }
            if thread.is_finished() {
                let _ = thread.join();
            }
        }
    }
}

const REPLY_TIMEOUT: Duration = Duration::from_secs(5);

impl Ptt {
    /// Key until released or until `deadline`, whichever comes first.
    pub fn key(&self, deadline: Instant) -> anyhow::Result<()> {
        let (reply, answer) = mpsc::channel();
        self.commands
            .send(Command::Key { deadline, reply })
            .map_err(|_| anyhow::anyhow!("PTT supervisor is not running"))?;
        answer
            .recv_timeout(REPLY_TIMEOUT)
            .map_err(|_| anyhow::anyhow!("PTT did not respond"))?
    }

    /// Release. Returns whether the deadline had already released it.
    pub fn release(&self) -> anyhow::Result<bool> {
        let (reply, answer) = mpsc::channel();
        self.commands
            .send(Command::Release { reply })
            .map_err(|_| anyhow::anyhow!("PTT supervisor is not running"))?;
        answer
            .recv_timeout(REPLY_TIMEOUT)
            .map_err(|_| anyhow::anyhow!("PTT release was not confirmed"))?
    }

    /// Release immediately and refuse further keying. Safe from any thread.
    pub fn stop(&self) {
        let _ = self.commands.send(Command::Stop);
    }
}

fn supervise(mut line: Box<dyn PttLine>, commands: mpsc::Receiver<Command>) {
    let mut keyed_until: Option<Instant> = None;
    let mut capped = false;
    let mut fault: Option<String> = None;
    loop {
        let command = match keyed_until {
            Some(deadline) => {
                match commands.recv_timeout(deadline.saturating_duration_since(Instant::now())) {
                    Ok(c) => Some(c),
                    Err(mpsc::RecvTimeoutError::Timeout) => None,
                    Err(mpsc::RecvTimeoutError::Disconnected) => Some(Command::Stop),
                }
            }
            None => match commands.recv() {
                Ok(c) => Some(c),
                Err(_) => Some(Command::Stop),
            },
        };
        match command {
            // The deadline passed while keyed.
            None => {
                keyed_until = None;
                capped = true;
                if let Err(err) = line.release() {
                    fault = Some(format!("release at the transmit cap failed: {err:#}"));
                    ui::error!(
                        "PTT release failed at the transmit cap: {err:#}. Turn the radio off."
                    );
                }
            }
            Some(Command::Key { deadline, reply }) => {
                let result = if let Some(fault) = &fault {
                    Err(anyhow::anyhow!("PTT is faulted ({fault})"))
                } else if deadline <= Instant::now() {
                    Err(anyhow::anyhow!("transmit deadline already passed"))
                } else {
                    // Mark keyed before asserting: a partial assertion still needs release.
                    keyed_until = Some(deadline);
                    capped = false;
                    line.key().inspect_err(|err| {
                        fault = Some(format!("{err:#}"));
                        keyed_until = None;
                        let _ = line.release();
                    })
                };
                let _ = reply.send(result);
            }
            Some(Command::Release { reply }) => {
                keyed_until = None;
                let result = match line.release() {
                    Ok(()) => Ok(capped),
                    Err(err) => {
                        fault = Some(format!("{err:#}"));
                        Err(err)
                    }
                };
                let _ = reply.send(result);
            }
            Some(Command::Stop) => {
                // Later requests fail because the supervisor is gone.
                if let Err(err) = line.release() {
                    ui::error!("PTT release failed while stopping: {err:#}. Turn the radio off.");
                }
                return;
            }
        }
    }
}

#[cfg(test)]
pub mod fake {
    use super::*;
    use std::sync::{Arc, Mutex};

    /// Records every line change with its time.
    #[derive(Clone, Default)]
    pub struct FakeLine {
        pub events: Arc<Mutex<Vec<(bool, Instant)>>>,
        pub fail_key: Arc<Mutex<bool>>,
        pub fail_release: Arc<Mutex<bool>>,
    }

    impl FakeLine {
        pub fn keyed(&self) -> bool {
            self.events.lock().unwrap().last().is_some_and(|(k, _)| *k)
        }

        pub fn changes(&self) -> Vec<bool> {
            self.events
                .lock()
                .unwrap()
                .iter()
                .map(|(k, _)| *k)
                .collect()
        }
    }

    impl PttLine for FakeLine {
        fn key(&mut self) -> anyhow::Result<()> {
            self.events.lock().unwrap().push((true, Instant::now()));
            if *self.fail_key.lock().unwrap() {
                bail!("simulated key failure");
            }
            Ok(())
        }

        fn release(&mut self) -> anyhow::Result<()> {
            if *self.fail_release.lock().unwrap() {
                bail!("simulated release failure");
            }
            self.events.lock().unwrap().push((false, Instant::now()));
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::fake::FakeLine;
    use super::*;

    fn soon(ms: u64) -> Instant {
        Instant::now() + Duration::from_millis(ms)
    }

    #[test]
    fn deadline_releases_without_any_request() {
        let line = FakeLine::default();
        let owner = PttOwner::start(Box::new(line.clone()));
        let ptt = owner.handle();
        ptt.key(soon(50)).unwrap();
        assert!(line.keyed());
        std::thread::sleep(Duration::from_millis(120));
        assert!(!line.keyed(), "the cap must release the line on its own");
        assert!(ptt.release().unwrap(), "release reports the cap was hit");
    }

    #[test]
    fn key_failure_faults_and_releases() {
        let line = FakeLine::default();
        *line.fail_key.lock().unwrap() = true;
        let owner = PttOwner::start(Box::new(line.clone()));
        let ptt = owner.handle();
        assert!(ptt.key(soon(1000)).is_err());
        assert!(!line.keyed());
        *line.fail_key.lock().unwrap() = false;
        assert!(
            ptt.key(soon(1000)).is_err(),
            "a faulted line never keys again"
        );
    }

    #[test]
    fn release_failure_is_reported_and_faults() {
        let line = FakeLine::default();
        let owner = PttOwner::start(Box::new(line.clone()));
        let ptt = owner.handle();
        ptt.key(soon(1000)).unwrap();
        *line.fail_release.lock().unwrap() = true;
        assert!(ptt.release().is_err());
        *line.fail_release.lock().unwrap() = false;
        assert!(ptt.key(soon(1000)).is_err());
    }

    #[test]
    fn stop_releases_and_refuses_new_keys() {
        let line = FakeLine::default();
        let owner = PttOwner::start(Box::new(line.clone()));
        let ptt = owner.handle();
        ptt.key(soon(5000)).unwrap();
        ptt.stop();
        std::thread::sleep(Duration::from_millis(30));
        assert!(!line.keyed());
        assert!(ptt.key(soon(5000)).is_err());
    }

    #[test]
    fn dropping_the_owner_releases() {
        let line = FakeLine::default();
        let owner = PttOwner::start(Box::new(line.clone()));
        owner.handle().key(soon(5000)).unwrap();
        drop(owner);
        assert!(!line.keyed());
    }
}
