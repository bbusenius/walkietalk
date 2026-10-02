//! Local speech with the Piper executable.

use std::path::PathBuf;

use anyhow::{Context, bail};
use async_trait::async_trait;

use super::{Shaping, Voice, check_text};
use crate::audio::{Clip, Fit};
use crate::exec::{self, Job};

pub struct Piper {
    executable: String,
    model: PathBuf,
    shaping: Shaping,
}

impl Piper {
    pub fn new(executable: &str, model: PathBuf, shaping: Shaping) -> Piper {
        Piper {
            executable: executable.to_string(),
            model,
            shaping,
        }
    }

    fn check(&self) -> anyhow::Result<PathBuf> {
        let program = exec::find(&self.executable).context(
            "Piper is not installed; see the installation guide or set tts.piper.executable",
        )?;
        let config = PathBuf::from(format!("{}.json", self.model.display()));
        for file in [&self.model, &config] {
            if !file.is_file() {
                bail!(
                    "Piper voice file missing: {}; download the voice (.onnx and .onnx.json)",
                    file.display()
                );
            }
        }
        Ok(program)
    }
}

#[async_trait]
impl Voice for Piper {
    fn label(&self) -> String {
        let name = self
            .model
            .file_stem()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_default();
        format!("piper ({name}, local)")
    }

    async fn prepare(&self) -> anyhow::Result<()> {
        self.check().map(|_| ())
    }

    async fn synthesize(&self, text: &str, fit: Fit) -> anyhow::Result<Clip> {
        let text = check_text(text)?;
        let program = self.check()?;
        let dir = tempfile::Builder::new()
            .prefix("walkietalk-piper-")
            .tempdir()?;
        let out = dir.path().join("speech.wav");
        let output = Job::new(program, "Piper")
            .args([
                "-m".as_ref(),
                self.model.as_os_str(),
                "-f".as_ref(),
                out.as_os_str(),
            ])
            .env(exec::inherit(&[
                "HOME",
                "PATH",
                "LANG",
                "LC_ALL",
                "LD_LIBRARY_PATH",
            ]))
            .cwd(dir.path())
            .stdin(format!("{text}\n").into_bytes())
            .timeout(self.shaping.timeout)
            .max_output(256 * 1024)
            .run()
            .await?;
        if !output.success() {
            bail!(
                "Piper failed (exit {}); check the voice model",
                output.status.code().unwrap_or(-1)
            );
        }
        let clip = Clip::read_wav(&out, self.shaping.decode_limit()).context("Piper output")?;
        self.shaping.finish(clip, fit)
    }
}
