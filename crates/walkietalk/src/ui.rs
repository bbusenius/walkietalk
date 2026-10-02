//! User-facing log lines.
//!
//! Every line has a kind that decides its color. Normally lines go to the
//! terminal; the operator panel installs a sink to show them in its log pane.

use std::sync::{Mutex, OnceLock};

use anstyle::{AnsiColor, Style};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Status,
    /// Frequent, low-importance updates such as the level meter.
    Meter,
    Event,
    Transcript,
    Accepted,
    Ignored,
    Reply,
    Warn,
    Error,
}

impl Kind {
    fn style(self) -> Style {
        let color = |c: AnsiColor| Style::new().fg_color(Some(c.into()));
        match self {
            Kind::Status => Style::new().bold(),
            Kind::Meter => Style::new().dimmed(),
            Kind::Event => color(AnsiColor::Cyan),
            Kind::Transcript => color(AnsiColor::Cyan).bold(),
            Kind::Accepted => color(AnsiColor::Green).bold(),
            Kind::Ignored | Kind::Warn => color(AnsiColor::Yellow),
            Kind::Reply => color(AnsiColor::Magenta),
            Kind::Error => color(AnsiColor::Red).bold(),
        }
    }
}

type Sink = Box<dyn Fn(Kind, &str) + Send + Sync>;

fn sink() -> &'static Mutex<Option<Sink>> {
    static SINK: OnceLock<Mutex<Option<Sink>>> = OnceLock::new();
    SINK.get_or_init(|| Mutex::new(None))
}

/// Route lines to `f` until [`clear_sink`] is called.
pub fn set_sink(f: impl Fn(Kind, &str) + Send + Sync + 'static) {
    *sink().lock().unwrap_or_else(|e| e.into_inner()) = Some(Box::new(f));
}

pub fn clear_sink() {
    *sink().lock().unwrap_or_else(|e| e.into_inner()) = None;
}

pub fn emit(kind: Kind, message: &str) {
    let guard = sink().lock().unwrap_or_else(|e| e.into_inner());
    if let Some(sink) = guard.as_ref() {
        sink(kind, message);
        return;
    }
    drop(guard);
    let style = kind.style();
    if matches!(kind, Kind::Warn | Kind::Error) {
        anstream::eprintln!("{style}{message}{style:#}");
    } else {
        anstream::println!("{style}{message}{style:#}");
    }
}

// One macro per kind: `ui::status!("format {}", args)`.
#[allow(unused_macros)]
macro_rules! status {
    ($($arg:tt)*) => { $crate::ui::emit($crate::ui::Kind::Status, &format!($($arg)*)) };
}
#[allow(unused_imports)]
pub(crate) use status;

#[allow(unused_macros)]
macro_rules! meter {
    ($($arg:tt)*) => { $crate::ui::emit($crate::ui::Kind::Meter, &format!($($arg)*)) };
}
#[allow(unused_imports)]
pub(crate) use meter;

#[allow(unused_macros)]
macro_rules! event {
    ($($arg:tt)*) => { $crate::ui::emit($crate::ui::Kind::Event, &format!($($arg)*)) };
}
#[allow(unused_imports)]
pub(crate) use event;

#[allow(unused_macros)]
macro_rules! transcript {
    ($($arg:tt)*) => { $crate::ui::emit($crate::ui::Kind::Transcript, &format!($($arg)*)) };
}
#[allow(unused_imports)]
pub(crate) use transcript;

#[allow(unused_macros)]
macro_rules! accepted {
    ($($arg:tt)*) => { $crate::ui::emit($crate::ui::Kind::Accepted, &format!($($arg)*)) };
}
#[allow(unused_imports)]
pub(crate) use accepted;

#[allow(unused_macros)]
macro_rules! ignored {
    ($($arg:tt)*) => { $crate::ui::emit($crate::ui::Kind::Ignored, &format!($($arg)*)) };
}
#[allow(unused_imports)]
pub(crate) use ignored;

#[allow(unused_macros)]
macro_rules! reply {
    ($($arg:tt)*) => { $crate::ui::emit($crate::ui::Kind::Reply, &format!($($arg)*)) };
}
#[allow(unused_imports)]
pub(crate) use reply;

#[allow(unused_macros)]
macro_rules! warning {
    ($($arg:tt)*) => { $crate::ui::emit($crate::ui::Kind::Warn, &format!($($arg)*)) };
}
#[allow(unused_imports)]
pub(crate) use warning;

#[allow(unused_macros)]
macro_rules! error {
    ($($arg:tt)*) => { $crate::ui::emit($crate::ui::Kind::Error, &format!($($arg)*)) };
}
#[allow(unused_imports)]
pub(crate) use error;
