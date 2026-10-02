//! Local speech recognition with whisper.cpp.

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{Context, bail};
use async_trait::async_trait;
use whisper_rs::{FullParams, SamplingStrategy, WhisperContext, WhisperContextParameters};

use super::Transcriber;
use super::models;
use crate::audio::Clip;
use crate::audio::clip::resample_f32;
use crate::config::WhisperModel;

const WHISPER_RATE: u32 = 16_000;

pub struct Whisper {
    model: WhisperModel,
    path: PathBuf,
    timeout: Duration,
    context: Mutex<Option<Arc<WhisperContext>>>,
    /// Set while a transcription runs; a timed-out run is aborted.
    busy: Arc<AtomicBool>,
}

impl Whisper {
    pub fn new(model: WhisperModel, timeout: Duration) -> Whisper {
        Whisper {
            model,
            path: models::path(model),
            timeout,
            context: Mutex::new(None),
            busy: Arc::new(AtomicBool::new(false)),
        }
    }

    fn context(&self) -> anyhow::Result<Arc<WhisperContext>> {
        let mut slot = self.context.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(context) = slot.as_ref() {
            return Ok(context.clone());
        }
        if !self.path.is_file() {
            bail!("Whisper {} is not downloaded; run `walkietalk models`", self.model);
        }
        whisper_rs::install_logging_hooks();
        let path = self.path.to_str().context("model path must be UTF-8")?;
        let context = WhisperContext::new_with_params(path, WhisperContextParameters::default())
            .map_err(|err| anyhow::anyhow!("cannot load Whisper {}: {err}", self.model))?;
        let context = Arc::new(context);
        *slot = Some(context.clone());
        Ok(context)
    }
}

#[async_trait]
impl Transcriber for Whisper {
    fn label(&self) -> String {
        format!("whisper ({}, local)", self.model)
    }

    async fn prepare(&self) -> anyhow::Result<()> {
        self.context().map(|_| ())
    }

    async fn transcribe(&self, audio: &Clip) -> anyhow::Result<String> {
        let context = self.context()?;
        if self.busy.swap(true, Ordering::SeqCst) {
            bail!("Whisper is still finishing the previous audio; this utterance was skipped");
        }
        let input = for_whisper(audio);
        let cancel = Arc::new(AtomicBool::new(false));
        let (busy, flag) = (self.busy.clone(), cancel.clone());
        let job = tokio::task::spawn_blocking(move || {
            let result = run(&context, &input, flag);
            busy.store(false, Ordering::SeqCst);
            result
        });
        match tokio::time::timeout(self.timeout, job).await {
            Ok(joined) => joined.context("Whisper worker failed")?,
            Err(_) => {
                // whisper.cpp checks this flag and stops early.
                cancel.store(true, Ordering::SeqCst);
                bail!("Whisper timed out after {:.0}s", self.timeout.as_secs_f64())
            }
        }
    }
}

fn run(context: &WhisperContext, samples: &[f32], cancel: Arc<AtomicBool>) -> anyhow::Result<String> {
    if samples.len() < (WHISPER_RATE / 20) as usize {
        return Ok(String::new());
    }
    let mut state = context.create_state().map_err(|err| anyhow::anyhow!("Whisper state: {err}"))?;
    let mut params = FullParams::new(SamplingStrategy::BeamSearch { beam_size: 5, patience: -1.0 });
    params.set_language(Some("en"));
    params.set_no_context(true);
    params.set_print_special(false);
    params.set_print_progress(false);
    params.set_print_realtime(false);
    params.set_print_timestamps(false);
    params.set_suppress_blank(true);
    let threads = std::thread::available_parallelism().map_or(4, |n| n.get().min(8));
    params.set_n_threads(threads as i32);
    params.set_abort_callback_safe(move || cancel.load(Ordering::SeqCst));
    state
        .full(params, samples)
        .map_err(|err| anyhow::anyhow!("Whisper failed: {err}"))?;
    let text: Vec<String> = state
        .as_iter()
        .filter_map(|segment| segment.to_str_lossy().ok().map(|s| s.trim().to_string()))
        .filter(|s| !s.is_empty())
        .collect();
    Ok(text.join(" "))
}

/// Convert captured audio to Whisper's 16 kHz float samples.
fn for_whisper(audio: &Clip) -> Vec<f32> {
    let input: Vec<f32> = audio.samples().iter().map(|&s| s as f32 / 32768.0).collect();
    resample_f32(&input, audio.rate(), WHISPER_RATE)
}
