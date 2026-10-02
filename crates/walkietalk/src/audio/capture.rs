//! Live capture from the radio interface, delivered as 20 ms frames.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, mpsc as std_mpsc};
use std::time::Duration;

use anyhow::{Context, bail};
use cpal::traits::{DeviceTrait, StreamTrait};
use cpal::{FromSample, Sample, SampleFormat, SizedSample};
use tokio::sync::mpsc;

use super::device::{self, Direction};
use super::vad::frame_len;
use crate::config::RADIO_RATE;

/// Audio right after opening a device can contain clicks; skip it.
const SETTLE: Duration = Duration::from_millis(200);
/// About ten seconds of frames may queue before audio is dropped.
const QUEUE_FRAMES: usize = 500;

/// An open capture stream. Dropping it closes the device.
pub struct Capture {
    frames: mpsc::Receiver<Vec<i16>>,
    rate: u32,
    overflow: Arc<AtomicBool>,
    failure: Arc<Mutex<Option<String>>>,
    _stop: std_mpsc::Sender<()>,
}

impl Capture {
    pub fn open(name: &str) -> anyhow::Result<Capture> {
        let (ready_tx, ready_rx) = std_mpsc::channel();
        let (stop_tx, stop_rx) = std_mpsc::channel::<()>();
        let (frame_tx, frames) = mpsc::channel(QUEUE_FRAMES);
        let overflow = Arc::new(AtomicBool::new(false));
        let failure = Arc::new(Mutex::new(None));
        let name_owned = name.to_string();
        let (o, f) = (overflow.clone(), failure.clone());
        // The stream lives on its own thread so a stuck driver can never
        // block the caller, even while closing.
        std::thread::Builder::new()
            .name("capture".into())
            .spawn(move || {
                let stream = match build(&name_owned, frame_tx, o, f) {
                    Ok((stream, rate)) => {
                        let _ = ready_tx.send(Ok(rate));
                        stream
                    }
                    Err(err) => {
                        let _ = ready_tx.send(Err(err));
                        return;
                    }
                };
                let _ = stop_rx.recv();
                drop(stream);
            })
            .context("cannot start capture thread")?;
        let rate = ready_rx
            .recv_timeout(Duration::from_secs(10))
            .map_err(|_| anyhow::anyhow!("capture device \"{name}\" did not open within 10 s"))??;
        Ok(Capture {
            frames,
            rate,
            overflow,
            failure,
            _stop: stop_tx,
        })
    }

    pub fn rate(&self) -> u32 {
        self.rate
    }

    /// The next 20 ms frame. Fails if the device stopped working.
    pub async fn next_frame(&mut self) -> anyhow::Result<Vec<i16>> {
        match self.frames.recv().await {
            Some(frame) if !frame.is_empty() => Ok(frame),
            _ => {
                let reason = self
                    .failure
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .take();
                bail!(
                    "capture stopped: {}",
                    reason.unwrap_or_else(|| "device closed".into())
                )
            }
        }
    }

    /// Whether audio was dropped since the last call.
    pub fn take_overflow(&self) -> bool {
        self.overflow.swap(false, Ordering::Relaxed)
    }
}

type Built = (cpal::Stream, u32);

fn build(
    name: &str,
    frames: mpsc::Sender<Vec<i16>>,
    overflow: Arc<AtomicBool>,
    failure: Arc<Mutex<Option<String>>>,
) -> anyhow::Result<Built> {
    let device = device::find(name, Direction::Input)?;
    let chosen = device::choose(&device, Direction::Input, RADIO_RATE, name)?;
    let rate = chosen.config.sample_rate;
    let stream = match chosen.format {
        SampleFormat::I16 => stream::<i16>(&device, chosen.config, frames, overflow, failure, name),
        _ => stream::<f32>(&device, chosen.config, frames, overflow, failure, name),
    }?;
    stream.play().map_err(|err| device::busy_hint(name, err))?;
    Ok((stream, rate))
}

fn stream<T>(
    device: &cpal::Device,
    config: cpal::StreamConfig,
    frames: mpsc::Sender<Vec<i16>>,
    overflow: Arc<AtomicBool>,
    failure: Arc<Mutex<Option<String>>>,
    name: &str,
) -> anyhow::Result<cpal::Stream>
where
    T: SizedSample + Send + 'static,
    i16: FromSample<T>,
{
    let channels = config.channels as usize;
    let size = frame_len(config.sample_rate);
    let mut skip = (SETTLE.as_secs_f64() * config.sample_rate as f64) as usize;
    let mut pending: Vec<i16> = Vec::with_capacity(size);
    let error_slot = failure.clone();
    let wake = frames.clone();
    let lost = overflow.clone();
    device
        .build_input_stream(
            config,
            move |data: &[T], _| {
                for chunk in data.chunks(channels) {
                    if skip > 0 {
                        skip -= 1;
                        continue;
                    }
                    pending.push(i16::from_sample(chunk[0]));
                    if pending.len() == size {
                        let frame = std::mem::replace(&mut pending, Vec::with_capacity(size));
                        if frames.try_send(frame).is_err() {
                            overflow.store(true, Ordering::Relaxed);
                        }
                    }
                }
            },
            move |err| {
                // An overrun loses some audio; the stream recovers by itself.
                if err.kind() == cpal::ErrorKind::Xrun {
                    lost.store(true, Ordering::Relaxed);
                    return;
                }
                *error_slot.lock().unwrap_or_else(|e| e.into_inner()) = Some(err.to_string());
                // An empty frame tells the reader the stream failed.
                let _ = wake.try_send(Vec::new());
            },
            None,
        )
        .map_err(|err| device::busy_hint(name, err))
}
