//! Resampling audio that arrives in pieces.

use rubato::audioadapter_buffers::direct::InterleavedSlice;
use rubato::{Fft, FixedSync, Indexing, Resampler};

use super::clip::to_i16;

/// Converts a stream of mono samples between two fixed rates.
pub struct StreamResampler {
    inner: Option<Fft<f32>>,
    pending: Vec<f32>,
}

impl StreamResampler {
    pub fn new(from: u32, to: u32) -> StreamResampler {
        let inner = (from != to).then(|| {
            let chunk = (from as usize / 50).max(64);
            Fft::<f32>::new(from as usize, to as usize, chunk, 1, FixedSync::Input)
                .expect("valid resampler parameters")
        });
        StreamResampler {
            inner,
            pending: Vec::new(),
        }
    }

    /// Add samples; returns whatever output is ready.
    pub fn push(&mut self, samples: &[i16]) -> Vec<i16> {
        let Some(inner) = self.inner.as_mut() else {
            return samples.to_vec();
        };
        self.pending
            .extend(samples.iter().map(|&s| s as f32 / 32768.0));
        let mut out = Vec::new();
        loop {
            let need = inner.input_frames_next();
            if self.pending.len() < need {
                return out;
            }
            let chunk: Vec<f32> = self.pending.drain(..need).collect();
            let input = InterleavedSlice::new(&chunk, 1, need).expect("mono adapter");
            let produced = inner
                .process(&input, None)
                .expect("resampling a full chunk");
            out.extend(produced.take_data().into_iter().map(to_i16));
        }
    }

    /// Emit the remainder, padded with silence.
    pub fn flush(&mut self) -> Vec<i16> {
        let Some(inner) = self.inner.as_mut() else {
            return Vec::new();
        };
        if self.pending.is_empty() {
            return Vec::new();
        }
        let need = inner.input_frames_next();
        let len = self.pending.len().min(need);
        let mut chunk: Vec<f32> = self.pending.drain(..len).collect();
        chunk.resize(need, 0.0);
        let input = InterleavedSlice::new(&chunk, 1, need).expect("mono adapter");
        let ratio = inner.resample_ratio();
        let produced = inner
            .process(&input, Some(&Indexing::new().partial_len(len)))
            .expect("resampling the final chunk");
        let keep = ((len as f64) * ratio).round() as usize;
        produced
            .take_data()
            .into_iter()
            .take(keep)
            .map(to_i16)
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn halving_and_doubling_keep_length() {
        let input = vec![1000i16; 48_000];
        let mut down = StreamResampler::new(48_000, 24_000);
        let mut out = Vec::new();
        for piece in input.chunks(960) {
            out.extend(down.push(piece));
        }
        out.extend(down.flush());
        assert!((out.len() as i64 - 24_000).abs() < 600, "{}", out.len());

        let mut up = StreamResampler::new(24_000, 48_000);
        let mut doubled = up.push(&out);
        doubled.extend(up.flush());
        assert!(
            (doubled.len() as i64 - 48_000).abs() < 1200,
            "{}",
            doubled.len()
        );
    }

    #[test]
    fn same_rate_passes_through() {
        let mut same = StreamResampler::new(24_000, 24_000);
        assert_eq!(same.push(&[1, 2, 3]), vec![1, 2, 3]);
        assert!(same.flush().is_empty());
    }
}
