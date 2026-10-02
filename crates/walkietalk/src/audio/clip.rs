//! Mono 16-bit audio clips and the operations the radio path needs.

use std::io::Cursor;
use std::path::Path;
use std::time::Duration;

use anyhow::{Context, bail};
use rubato::audioadapter_buffers::direct::InterleavedSlice;
use rubato::{Fft, FixedSync, Resampler};

use crate::config::RADIO_RATE;

/// Mono 16-bit PCM at a known rate.
#[derive(Clone, PartialEq, Eq)]
pub struct Clip {
    samples: Vec<i16>,
    rate: u32,
}

impl std::fmt::Debug for Clip {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Clip({} Hz, {:.3}s)", self.rate, self.seconds())
    }
}

/// A clip is longer than the space it must fit.
#[derive(Debug, Clone, Copy, PartialEq, thiserror::Error)]
#[error("audio is {:.1}s; at most {:.1}s fits", .actual.as_secs_f64(), .limit.as_secs_f64())]
pub struct TooLong {
    pub actual: Duration,
    pub limit: Duration,
}

/// How to treat a clip that is longer than its budget.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Fit {
    /// Cut the end off.
    Crop,
    /// Refuse it. Used for station IDs, which must never be shortened.
    Strict,
}

impl Clip {
    pub fn new(samples: Vec<i16>, rate: u32) -> Clip {
        assert!(rate > 0, "sample rate must be positive");
        Clip { samples, rate }
    }

    pub fn silence(duration: Duration, rate: u32) -> Clip {
        Clip::new(vec![0; frames_for(duration, rate)], rate)
    }

    pub fn samples(&self) -> &[i16] {
        &self.samples
    }

    pub fn into_samples(self) -> Vec<i16> {
        self.samples
    }

    pub fn rate(&self) -> u32 {
        self.rate
    }

    pub fn len(&self) -> usize {
        self.samples.len()
    }

    pub fn is_empty(&self) -> bool {
        self.samples.is_empty()
    }

    pub fn duration(&self) -> Duration {
        Duration::from_secs_f64(self.seconds())
    }

    pub fn seconds(&self) -> f64 {
        self.samples.len() as f64 / self.rate as f64
    }

    /// Convert to `rate` with band-limited resampling.
    pub fn resample(&self, rate: u32) -> Clip {
        if rate == self.rate || self.samples.is_empty() {
            return Clip::new(self.samples.clone(), rate);
        }
        let input: Vec<f32> = self.samples.iter().map(|&s| s as f32 / 32768.0).collect();
        let output = resample_f32(&input, self.rate, rate);
        Clip::new(output.into_iter().map(to_i16).collect(), rate)
    }

    /// The clip at the radio's sample rate.
    pub fn to_radio(&self) -> Clip {
        self.resample(RADIO_RATE)
    }

    /// Make the clip fit in `limit`, cropping or refusing as `fit` says.
    pub fn fit(mut self, limit: Duration, fit: Fit) -> Result<Clip, TooLong> {
        let max = frames_for(limit, self.rate);
        if self.samples.len() <= max {
            return Ok(self);
        }
        match fit {
            Fit::Strict => Err(TooLong {
                actual: self.duration(),
                limit,
            }),
            Fit::Crop => {
                self.samples.truncate(max);
                Ok(self)
            }
        }
    }

    /// Multiply by `gain`, saturating instead of wrapping.
    pub fn with_gain(mut self, gain: f64) -> Clip {
        if gain != 1.0 {
            for s in &mut self.samples {
                *s = (*s as f64 * gain).round().clamp(i16::MIN as f64, i16::MAX as f64) as i16;
            }
        }
        self
    }

    /// Scale so the loudest sample reaches full scale.
    pub fn peak_normalized(self) -> Clip {
        let peak = self
            .samples
            .iter()
            .map(|s| s.unsigned_abs())
            .max()
            .unwrap_or(0);
        if peak == 0 || peak >= i16::MAX as u16 {
            return self;
        }
        let gain = i16::MAX as f64 / peak as f64;
        self.with_gain(gain)
    }

    /// Append another clip at the same rate.
    pub fn append(&mut self, other: &Clip) {
        assert_eq!(self.rate, other.rate, "clips must share a sample rate");
        self.samples.extend_from_slice(&other.samples);
    }

    /// Root-mean-square level, 0 to 1.
    pub fn rms(&self) -> f64 {
        rms(&self.samples)
    }

    /// Read a mono 16-bit PCM WAV, refusing anything longer than `max`.
    pub fn read_wav(path: &Path, max: Duration) -> anyhow::Result<Clip> {
        let reader = hound::WavReader::open(path).with_context(|| format!("cannot read WAV {}", path.display()))?;
        Self::from_reader(reader, max).with_context(|| format!("WAV {}", path.display()))
    }

    /// Decode an in-memory WAV. Streaming encoders often write a
    /// placeholder data length; it is corrected to the bytes present.
    pub fn from_wav_bytes(bytes: &[u8], max: Duration) -> anyhow::Result<Clip> {
        let mut bytes = bytes.to_vec();
        fix_streaming_lengths(&mut bytes);
        Self::from_reader(hound::WavReader::new(Cursor::new(bytes))?, max)
    }

    fn from_reader<R: std::io::Read>(reader: hound::WavReader<R>, max: Duration) -> anyhow::Result<Clip> {
        let spec = reader.spec();
        if spec.channels != 1 || spec.bits_per_sample != 16 || spec.sample_format != hound::SampleFormat::Int {
            bail!("use an uncompressed mono 16-bit PCM WAV");
        }
        if !(8_000..=48_000).contains(&spec.sample_rate) {
            bail!("sample rate {} Hz is not supported; use 8000 to 48000 Hz", spec.sample_rate);
        }
        let limit = frames_for(max, spec.sample_rate);
        // Some streaming encoders write a placeholder length; trust the data, bounded.
        let mut samples = Vec::new();
        for sample in reader.into_samples::<i16>() {
            if samples.len() == limit {
                bail!(TooLong {
                    actual: Duration::from_secs_f64((limit + 1) as f64 / spec.sample_rate as f64),
                    limit: max,
                });
            }
            samples.push(sample.context("truncated WAV data")?);
        }
        if samples.is_empty() {
            bail!("the WAV contains no audio");
        }
        Ok(Clip::new(samples, spec.sample_rate))
    }

    pub fn to_wav_bytes(&self) -> Vec<u8> {
        let mut cursor = Cursor::new(Vec::new());
        {
            let mut writer = hound::WavWriter::new(&mut cursor, self.spec()).expect("in-memory WAV writer");
            for &s in &self.samples {
                writer.write_sample(s).expect("in-memory WAV write");
            }
            writer.finalize().expect("in-memory WAV finalize");
        }
        cursor.into_inner()
    }

    /// Write a new WAV; an existing file is never replaced.
    pub fn write_new_wav(&self, path: &Path) -> anyhow::Result<()> {
        let file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(path)
            .with_context(|| format!("cannot create {} (it must not already exist)", path.display()))?;
        let mut writer = hound::WavWriter::new(std::io::BufWriter::new(file), self.spec())?;
        for &s in &self.samples {
            writer.write_sample(s)?;
        }
        writer.finalize()?;
        Ok(())
    }

    fn spec(&self) -> hound::WavSpec {
        hound::WavSpec {
            channels: 1,
            sample_rate: self.rate,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        }
    }
}

/// Clamp the RIFF and data chunk lengths to what is actually present.
fn fix_streaming_lengths(bytes: &mut [u8]) {
    if bytes.len() < 12 || &bytes[0..4] != b"RIFF" || &bytes[8..12] != b"WAVE" {
        return;
    }
    let total = bytes.len();
    let riff = (total - 8) as u32;
    if u32::from_le_bytes(bytes[4..8].try_into().expect("4 bytes")) as usize > total - 8 {
        bytes[4..8].copy_from_slice(&riff.to_le_bytes());
    }
    let mut pos = 12;
    while pos + 8 <= total {
        let size = u32::from_le_bytes(bytes[pos + 4..pos + 8].try_into().expect("4 bytes")) as usize;
        let start = pos + 8;
        if &bytes[pos..pos + 4] == b"data" {
            let present = (total - start) & !1;
            if size > present {
                bytes[pos + 4..pos + 8].copy_from_slice(&(present as u32).to_le_bytes());
            }
            return;
        }
        pos = start.saturating_add(size + (size & 1));
    }
}

pub fn frames_for(duration: Duration, rate: u32) -> usize {
    (duration.as_secs_f64() * rate as f64).floor() as usize
}

pub fn rms(samples: &[i16]) -> f64 {
    if samples.is_empty() {
        return 0.0;
    }
    let sum: f64 = samples.iter().map(|&s| (s as f64) * (s as f64)).sum();
    (sum / samples.len() as f64).sqrt() / 32768.0
}

pub fn to_i16(sample: f32) -> i16 {
    (sample * 32768.0).round().clamp(i16::MIN as f32, i16::MAX as f32) as i16
}

/// Resample a whole mono signal.
pub fn resample_f32(input: &[f32], from: u32, to: u32) -> Vec<f32> {
    if from == to || input.is_empty() {
        return input.to_vec();
    }
    let mut resampler = Fft::<f32>::new(from as usize, to as usize, 1024, 1, FixedSync::Input)
        .expect("valid resampler parameters");
    let adapter = InterleavedSlice::new(input, 1, input.len()).expect("mono adapter");
    resampler
        .process_all(&adapter, input.len(), None)
        .expect("resampling a mono buffer")
        .take_data()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tone(freq: f64, seconds: f64, rate: u32) -> Clip {
        let n = (seconds * rate as f64) as usize;
        Clip::new(
            (0..n)
                .map(|i| ((i as f64 / rate as f64 * freq * std::f64::consts::TAU).sin() * 16000.0) as i16)
                .collect(),
            rate,
        )
    }

    #[test]
    fn resampling_keeps_duration_and_level() {
        let clip = tone(440.0, 1.0, 22_050);
        let radio = clip.to_radio();
        assert_eq!(radio.rate(), 48_000);
        assert!((radio.seconds() - 1.0).abs() < 0.01, "{}", radio.seconds());
        assert!((radio.rms() - clip.rms()).abs() < 0.02);
    }

    #[test]
    fn strict_fit_refuses_and_crop_cuts() {
        let clip = Clip::silence(Duration::from_secs(3), 8000);
        assert!(clip.clone().fit(Duration::from_secs(2), Fit::Strict).is_err());
        let cropped = clip.fit(Duration::from_secs(2), Fit::Crop).unwrap();
        assert_eq!(cropped.len(), 16_000);
    }

    #[test]
    fn gain_saturates_without_wrapping() {
        let clip = Clip::new(vec![20_000, -20_000, 100], 8000).with_gain(2.0);
        assert_eq!(clip.samples(), &[i16::MAX, i16::MIN, 200]);
    }

    #[test]
    fn peak_normalization_reaches_full_scale() {
        let clip = Clip::new(vec![1000, -2000, 500], 8000).peak_normalized();
        assert_eq!(clip.samples().iter().map(|s| s.unsigned_abs()).max(), Some(32767));
        let silent = Clip::new(vec![0, 0], 8000).peak_normalized();
        assert_eq!(silent.samples(), &[0, 0]);
    }

    #[test]
    fn wav_round_trip_and_limits() {
        let clip = tone(300.0, 0.5, 16_000);
        let bytes = clip.to_wav_bytes();
        assert_eq!(Clip::from_wav_bytes(&bytes, Duration::from_secs(1)).unwrap(), clip);
        assert!(Clip::from_wav_bytes(&bytes, Duration::from_millis(100)).is_err());
    }

    #[test]
    fn streaming_placeholder_lengths_are_corrected() {
        let clip = tone(300.0, 0.25, 48_000);
        let mut bytes = clip.to_wav_bytes();
        bytes[4..8].copy_from_slice(&0xFFFF_FFFFu32.to_le_bytes());
        bytes[40..44].copy_from_slice(&0x7FFF_FFFFu32.to_le_bytes());
        assert_eq!(Clip::from_wav_bytes(&bytes, Duration::from_secs(1)).unwrap(), clip);
    }

    #[test]
    fn stereo_or_float_wavs_are_refused() {
        let mut cursor = Cursor::new(Vec::new());
        let spec = hound::WavSpec { channels: 2, sample_rate: 8000, bits_per_sample: 16, sample_format: hound::SampleFormat::Int };
        let mut w = hound::WavWriter::new(&mut cursor, spec).unwrap();
        w.write_sample(0i16).unwrap();
        w.write_sample(0i16).unwrap();
        w.finalize().unwrap();
        assert!(Clip::from_wav_bytes(&cursor.into_inner(), Duration::from_secs(1)).is_err());
    }

    #[test]
    fn writing_never_overwrites() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("a.wav");
        let clip = tone(300.0, 0.1, 8000);
        clip.write_new_wav(&path).unwrap();
        assert!(clip.write_new_wav(&path).is_err());
    }
}
