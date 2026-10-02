//! Downloading and locating local Whisper models.

use std::path::PathBuf;
use std::time::Duration;

use anyhow::{Context, bail};
use futures_util::StreamExt;
use sha2::{Digest, Sha256};
use tokio::io::AsyncWriteExt;

use crate::config::WhisperModel;
use crate::{paths, ui};

const BASE_URL: &str = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main";

fn file_name(model: WhisperModel) -> String {
    format!("ggml-{model}.en.bin")
}

/// Expected approximate size, for messages and a download ceiling.
fn approx_mb(model: WhisperModel) -> u64 {
    match model {
        WhisperModel::Tiny => 78,
        WhisperModel::Base => 148,
        WhisperModel::Small => 488,
    }
}

pub fn path(model: WhisperModel) -> PathBuf {
    paths::models_dir().join(file_name(model))
}

pub fn installed(model: WhisperModel) -> Option<u64> {
    std::fs::metadata(path(model)).ok().filter(|m| m.is_file()).map(|m| m.len())
}

/// Download the model unless it is already present. The file is checked
/// against the SHA-256 the server publishes, then moved into place.
pub async fn download(model: WhisperModel) -> anyhow::Result<PathBuf> {
    let dest = path(model);
    if dest.is_file() {
        ui::status!("Whisper {model} is already installed at {}", dest.display());
        return Ok(dest);
    }
    let dir = dest.parent().expect("model path has a parent");
    std::fs::create_dir_all(dir).with_context(|| format!("cannot create {}", dir.display()))?;
    let url = format!("{BASE_URL}/{}", file_name(model));
    ui::status!("Downloading Whisper {model} (about {} MB) to {}", approx_mb(model), dest.display());
    let client = reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(15))
        .read_timeout(Duration::from_secs(60))
        .build()?;
    let expected = published_checksum(&url).await;
    let response = client.get(&url).send().await.context("cannot reach the model server")?;
    if !response.status().is_success() {
        bail!("model download failed: HTTP {}", response.status());
    }
    let ceiling = approx_mb(model) * 1024 * 1024 * 2;
    let partial = dir.join(format!(".{}.partial", file_name(model)));
    let mut file = tokio::fs::File::create(&partial).await?;
    let mut hasher = Sha256::new();
    let mut received: u64 = 0;
    let mut last_report = 0;
    let mut body = response.bytes_stream();
    while let Some(chunk) = body.next().await {
        let chunk = chunk.context("model download interrupted")?;
        received += chunk.len() as u64;
        if received > ceiling {
            let _ = tokio::fs::remove_file(&partial).await;
            bail!("model download is larger than expected; aborted");
        }
        hasher.update(&chunk);
        file.write_all(&chunk).await?;
        let mb = received / (1024 * 1024);
        if mb >= last_report + 25 {
            last_report = mb;
            ui::meter!("  {mb} MB");
        }
    }
    file.flush().await?;
    drop(file);
    let actual = format!("{:x}", hasher.finalize());
    match expected {
        Some(expected) if expected != actual => {
            let _ = tokio::fs::remove_file(&partial).await;
            bail!("downloaded model failed its checksum; try again");
        }
        Some(_) => ui::status!("Checksum verified."),
        None => ui::warning!("The server published no checksum; size {received} bytes."),
    }
    tokio::fs::rename(&partial, &dest).await?;
    ui::status!("Installed Whisper {model}.");
    Ok(dest)
}

/// The SHA-256 Hugging Face publishes for a file, from the redirect it sends
/// before the download itself.
async fn published_checksum(url: &str) -> Option<String> {
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(15))
        .build()
        .ok()?;
    let response = client.head(url).send().await.ok()?;
    response
        .headers()
        .get("x-linked-etag")
        .and_then(|v| v.to_str().ok())
        .map(|v| v.trim_matches('"').to_ascii_lowercase())
        .filter(|v| v.len() == 64 && v.chars().all(|c| c.is_ascii_hexdigit()))
}
