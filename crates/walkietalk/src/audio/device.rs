//! Audio device discovery and stream configuration.

use anyhow::{Context, bail};
use cpal::traits::{DeviceTrait, HostTrait};
use cpal::{Device, SampleFormat, StreamConfig};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Direction {
    Input,
    Output,
}

/// A device as listed by `walkietalk devices`.
#[derive(Debug, Clone)]
pub struct Listing {
    pub name: String,
    pub description: String,
    pub input: bool,
    pub output: bool,
}

impl Listing {
    /// Hardware PCMs addressed by card name, with format conversion. These
    /// survive reconnects and are the right choice for a radio interface.
    pub fn recommended(&self) -> bool {
        self.name.starts_with("plughw:CARD=")
            && !self.name["plughw:CARD=".len()..].starts_with(|c: char| c.is_ascii_digit())
    }
}

/// Every PCM device ALSA knows about.
pub fn list() -> anyhow::Result<Vec<Listing>> {
    let _quiet = alsa::Output::local_error_handler();
    let host = cpal::default_host();
    let mut out = Vec::new();
    for device in host.devices().context("cannot enumerate audio devices")? {
        let Ok(id) = device.id() else { continue };
        out.push(Listing {
            name: id.id().to_string(),
            description: device.description().map(|d| d.to_string()).unwrap_or_default(),
            input: device.supports_input(),
            output: device.supports_output(),
        });
    }
    Ok(out)
}

/// The device with exactly this name.
pub fn find(name: &str, direction: Direction) -> anyhow::Result<Device> {
    let _quiet = alsa::Output::local_error_handler();
    let host = cpal::default_host();
    let device = host
        .devices()
        .context("cannot enumerate audio devices")?
        .find(|d| d.id().is_ok_and(|id| id.id() == name));
    let Some(device) = device else {
        bail!("no audio device named \"{name}\"; run `walkietalk devices` and copy the exact name");
    };
    let supported = match direction {
        Direction::Input => device.supports_input(),
        Direction::Output => device.supports_output(),
    };
    if !supported {
        let what = if direction == Direction::Input { "capture" } else { "playback" };
        bail!("audio device \"{name}\" does not support {what}");
    }
    Ok(device)
}

/// The stream settings to use: the preferred rate when supported, the
/// fewest channels, and 16-bit samples when available.
#[derive(Debug, Clone, Copy)]
pub struct Chosen {
    pub config: StreamConfig,
    pub format: SampleFormat,
}

pub fn choose(device: &Device, direction: Direction, preferred_rate: u32, name: &str) -> anyhow::Result<Chosen> {
    let ranges: Vec<_> = match direction {
        Direction::Input => device.supported_input_configs().map(|r| r.collect()),
        Direction::Output => device.supported_output_configs().map(|r| r.collect()),
    }
    .map_err(|err| busy_hint(name, err))?;
    let score = |format: SampleFormat| match format {
        SampleFormat::I16 => 0,
        SampleFormat::F32 => 1,
        _ => 2,
    };
    let best = ranges
        .iter()
        .filter(|r| matches!(r.sample_format(), SampleFormat::I16 | SampleFormat::F32))
        .min_by_key(|r| {
            let has_rate = (r.min_sample_rate()..=r.max_sample_rate()).contains(&preferred_rate);
            (!has_rate, r.channels(), score(r.sample_format()))
        })
        .with_context(|| format!("audio device \"{name}\" offers no 16-bit or float format"))?;
    let rate = if (best.min_sample_rate()..=best.max_sample_rate()).contains(&preferred_rate) {
        preferred_rate
    } else {
        best.max_sample_rate().min(48_000).max(best.min_sample_rate())
    };
    Ok(Chosen {
        config: StreamConfig {
            channels: best.channels(),
            sample_rate: rate,
            buffer_size: cpal::BufferSize::Default,
        },
        format: best.sample_format(),
    })
}

/// Explain the usual reason a device cannot be opened.
pub fn busy_hint(name: &str, err: impl std::fmt::Display) -> anyhow::Error {
    anyhow::anyhow!(
        "cannot open audio device \"{name}\": {err}. It may be in use; close other audio programs \
         and disable the interface in the desktop sound settings"
    )
}
