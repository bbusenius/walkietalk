//! Playback to the radio interface.
//!
//! Playback is always prepared before the transmitter is keyed: the device
//! is opened and running (playing silence) and the audio is queued. Starting
//! then only flips a flag. Each stream lives on its own thread so a stuck
//! driver can never block the caller; the PTT supervisor enforces the
//! transmit cap independently of all of this.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, mpsc};
use std::time::{Duration, Instant};

use anyhow::{Context, bail};
use cpal::traits::{DeviceTrait, StreamTrait};
use cpal::{FromSample, Sample, SampleFormat, SizedSample};

use super::Clip;
use super::device::{self, Direction};
use crate::config::RADIO_RATE;

/// Silence played after the last sample so the device buffer drains.
const TAIL: Duration = Duration::from_millis(150);

/// Something that can play radio-rate audio.
pub trait AudioOut: Send + Sync {
    /// Open the device and queue a whole clip, without starting it.
    fn prepare(&self, clip: &Clip) -> anyhow::Result<Box<dyn Playback>>;
    /// Open the device for audio that arrives over time.
    fn prepare_stream(&self) -> anyhow::Result<(Feed, Box<dyn Playback>)>;
}

/// Prepared audio, waiting to start.
pub trait Playback: Send {
    fn start(&mut self) -> anyhow::Result<()>;
    /// Wait until playback finished (`true`) or `deadline` passed (`false`).
    fn wait(&mut self, deadline: Instant) -> anyhow::Result<bool>;
}

/// Audio queued for a stream, shared with the device callback.
#[derive(Clone, Default)]
pub struct Feed(Arc<FeedState>);

#[derive(Default)]
struct FeedState {
    queue: Mutex<VecDeque<i16>>,
    finished: AtomicBool,
}

impl Feed {
    /// Queue radio-rate samples.
    pub fn push(&self, samples: &[i16]) {
        self.0.queue.lock().unwrap_or_else(|e| e.into_inner()).extend(samples.iter().copied());
    }

    /// No more audio will arrive; playback ends when the queue drains.
    pub fn finish(&self) {
        self.0.finished.store(true, Ordering::Release);
    }

    pub fn queued(&self) -> usize {
        self.0.queue.lock().unwrap_or_else(|e| e.into_inner()).len()
    }

    fn take(&self, out: &mut [i16]) -> usize {
        let mut queue = self.0.queue.lock().unwrap_or_else(|e| e.into_inner());
        let n = out.len().min(queue.len());
        for (slot, sample) in out.iter_mut().zip(queue.drain(..n)) {
            *slot = sample;
        }
        n
    }

    fn drained(&self) -> bool {
        self.0.finished.load(Ordering::Acquire) && self.queued() == 0
    }
}

/// A real ALSA device.
pub struct DeviceOut {
    name: String,
}

impl DeviceOut {
    pub fn new(name: &str) -> DeviceOut {
        DeviceOut { name: name.to_string() }
    }
}

impl AudioOut for DeviceOut {
    fn prepare(&self, clip: &Clip) -> anyhow::Result<Box<dyn Playback>> {
        let (feed, playback) = self.prepare_stream()?;
        feed.push(clip.to_radio().samples());
        feed.finish();
        Ok(playback)
    }

    fn prepare_stream(&self) -> anyhow::Result<(Feed, Box<dyn Playback>)> {
        let feed = Feed::default();
        let started = Arc::new(AtomicBool::new(false));
        let (done_tx, done_rx) = mpsc::channel();
        let (ready_tx, ready_rx) = mpsc::channel();
        let (stop_tx, stop_rx) = mpsc::channel::<()>();
        let name = self.name.clone();
        let (f, s) = (feed.clone(), started.clone());
        std::thread::Builder::new()
            .name("playback".into())
            .spawn(move || match open(&name, f, s, done_tx) {
                Ok(stream) => {
                    let _ = ready_tx.send(Ok(()));
                    let _ = stop_rx.recv();
                    drop(stream);
                }
                Err(err) => {
                    let _ = ready_tx.send(Err(err));
                }
            })
            .context("cannot start playback thread")?;
        ready_rx
            .recv_timeout(Duration::from_secs(10))
            .map_err(|_| anyhow::anyhow!("playback device \"{}\" did not open within 10 s", self.name))??;
        Ok((
            feed,
            Box::new(DevicePlayback {
                started,
                done: done_rx,
                _stop: stop_tx,
            }),
        ))
    }
}

struct DevicePlayback {
    started: Arc<AtomicBool>,
    done: mpsc::Receiver<Result<(), String>>,
    _stop: mpsc::Sender<()>,
}

impl Playback for DevicePlayback {
    fn start(&mut self) -> anyhow::Result<()> {
        self.started.store(true, Ordering::Release);
        Ok(())
    }

    fn wait(&mut self, deadline: Instant) -> anyhow::Result<bool> {
        let timeout = deadline.saturating_duration_since(Instant::now());
        match self.done.recv_timeout(timeout) {
            Ok(Ok(())) => Ok(true),
            Ok(Err(err)) => bail!("audio playback failed: {err}"),
            Err(mpsc::RecvTimeoutError::Timeout) => Ok(false),
            Err(mpsc::RecvTimeoutError::Disconnected) => bail!("audio playback stopped unexpectedly"),
        }
    }
}

fn open(name: &str, feed: Feed, started: Arc<AtomicBool>, done: mpsc::Sender<Result<(), String>>) -> anyhow::Result<cpal::Stream> {
    let device = device::find(name, Direction::Output)?;
    let chosen = device::choose(&device, Direction::Output, RADIO_RATE, name)?;
    if chosen.config.sample_rate != RADIO_RATE {
        bail!("playback device \"{name}\" must support {RADIO_RATE} Hz; use its plughw: name");
    }
    let stream = match chosen.format {
        SampleFormat::I16 => output::<i16>(&device, chosen.config, feed, started, done, name),
        _ => output::<f32>(&device, chosen.config, feed, started, done, name),
    }?;
    stream.play().map_err(|err| device::busy_hint(name, err))?;
    Ok(stream)
}

fn output<T>(
    device: &cpal::Device,
    config: cpal::StreamConfig,
    feed: Feed,
    started: Arc<AtomicBool>,
    done: mpsc::Sender<Result<(), String>>,
    name: &str,
) -> anyhow::Result<cpal::Stream>
where
    T: SizedSample + FromSample<i16> + Send + 'static,
{
    let channels = config.channels as usize;
    let tail_frames = (TAIL.as_secs_f64() * config.sample_rate as f64) as usize;
    let mut tail = 0usize;
    let mut finished = false;
    let mut mono: Vec<i16> = Vec::new();
    let error_done = done.clone();
    device
        .build_output_stream(
            config,
            move |data: &mut [T], _| {
                let frames = data.len() / channels;
                mono.clear();
                mono.resize(frames, 0);
                if started.load(Ordering::Acquire) && !finished {
                    let got = feed.take(&mut mono);
                    if got < frames && feed.drained() {
                        tail += frames - got;
                        if tail >= tail_frames {
                            finished = true;
                            let _ = done.send(Ok(()));
                        }
                    }
                }
                for (frame, &sample) in data.chunks_mut(channels).zip(&mono) {
                    frame.fill(T::from_sample(sample));
                }
            },
            move |err| {
                let _ = error_done.send(Err(err.to_string()));
            },
            None,
        )
        .map_err(|err| device::busy_hint(name, err))
}

/// Plays nothing; waits as long as the audio would take. For dry runs.
pub struct DryOut;

impl AudioOut for DryOut {
    fn prepare(&self, clip: &Clip) -> anyhow::Result<Box<dyn Playback>> {
        let feed = Feed::default();
        feed.push(clip.to_radio().samples());
        feed.finish();
        Ok(Box::new(DryPlayback { feed, started: None }))
    }

    fn prepare_stream(&self) -> anyhow::Result<(Feed, Box<dyn Playback>)> {
        let feed = Feed::default();
        Ok((feed.clone(), Box::new(DryPlayback { feed, started: None })))
    }
}

struct DryPlayback {
    feed: Feed,
    started: Option<Instant>,
}

impl Playback for DryPlayback {
    fn start(&mut self) -> anyhow::Result<()> {
        self.started = Some(Instant::now());
        Ok(())
    }

    fn wait(&mut self, deadline: Instant) -> anyhow::Result<bool> {
        // Consume audio in real time until the feed is finished and drained.
        let step = Duration::from_millis(20);
        let mut chunk = vec![0i16; (RADIO_RATE as usize) / 50];
        loop {
            if self.feed.drained() {
                return Ok(true);
            }
            if Instant::now() >= deadline {
                return Ok(false);
            }
            self.feed.take(&mut chunk);
            std::thread::sleep(step.min(deadline.saturating_duration_since(Instant::now())));
        }
    }
}
