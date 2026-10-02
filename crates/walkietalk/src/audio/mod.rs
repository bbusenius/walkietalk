//! Audio clips, capture, playback, and signal processing.

pub mod capture;
pub mod clip;
pub mod device;
pub mod morse;
pub mod playback;
pub mod vad;

pub use clip::{Clip, Fit, TooLong};
