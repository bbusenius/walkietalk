//! Speech recognition backends. They turn audio into text and nothing
//! more; wake matching and replies happen elsewhere.

pub mod models;
pub mod whisper;

use async_trait::async_trait;

use crate::audio::Clip;

#[async_trait]
pub trait Transcriber: Send + Sync {
    fn label(&self) -> String;
    /// Load models or check credentials before the first use.
    async fn prepare(&self) -> anyhow::Result<()>;
    async fn transcribe(&self, audio: &Clip) -> anyhow::Result<String>;
}
