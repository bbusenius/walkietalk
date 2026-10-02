//! walkietalk-hermes-speech: speaks text with the TTS provider configured in
//! a Hermes installation, for walkietalk's `tts.backend = "hermes"`.
//!
//! The service is small on purpose: one bearer-protected endpoint, one
//! request at a time, strict size and duration limits. Only the call into
//! Hermes's `text_to_speech_tool` runs in Hermes's Python; conversion to the
//! radio's 48 kHz mono WAV is done here with ffmpeg.

mod speech;

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, bail};
use axum::Router;
use axum::body::Bytes;
use axum::extract::{DefaultBodyLimit, State};
use axum::http::{HeaderMap, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::post;
use clap::Parser;
use tokio::sync::Semaphore;

use speech::{Limits, Request, SpeechError};

const MAX_REQUEST_BYTES: usize = 16 * 1024;

#[derive(Parser)]
#[command(name = "walkietalk-hermes-speech", version, about = "Speak text with a Hermes installation's TTS provider")]
struct Args {
    /// The Hermes source directory (added to Python's import path)
    #[arg(long, value_name = "DIR")]
    hermes_root: PathBuf,
    /// Hermes's Python interpreter [default: <hermes-root>/.venv/bin/python]
    #[arg(long, value_name = "FILE")]
    python: Option<PathBuf>,
    #[arg(long, default_value = "127.0.0.1")]
    host: String,
    #[arg(long, default_value_t = 8643)]
    port: u16,
    /// Environment variable holding the service bearer token
    #[arg(long, default_value = "API_SERVER_KEY", value_name = "NAME")]
    token_env: String,
    /// Longest audio a request may ask for
    #[arg(long, default_value_t = 120.0, value_name = "SECONDS")]
    max_audio_seconds: f64,
    /// Time allowed for the provider plus conversion
    #[arg(long, default_value_t = 120.0, value_name = "SECONDS")]
    timeout_seconds: f64,
}

struct App {
    token: String,
    limits: Limits,
    busy: Semaphore,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let args = Args::parse();
    let token = load_token(&args.token_env)?;
    anyhow::ensure!(
        args.max_audio_seconds.is_finite() && args.max_audio_seconds > 0.0 && args.max_audio_seconds <= 600.0,
        "--max-audio-seconds must be greater than 0 and at most 600"
    );
    anyhow::ensure!(
        args.timeout_seconds.is_finite() && args.timeout_seconds > 0.0 && args.timeout_seconds <= 600.0,
        "--timeout-seconds must be greater than 0 and at most 600"
    );
    let root = args.hermes_root.canonicalize().context("--hermes-root does not exist")?;
    let python = args.python.unwrap_or_else(|| root.join(".venv/bin/python"));
    anyhow::ensure!(python.is_file(), "Hermes's Python was not found at {}; pass --python", python.display());
    let app = Arc::new(App {
        token,
        limits: Limits {
            hermes_root: root,
            python,
            max_audio: args.max_audio_seconds,
            timeout: Duration::from_secs_f64(args.timeout_seconds),
        },
        busy: Semaphore::new(1),
    });
    let address: SocketAddr = format!("{}:{}", args.host, args.port).parse().context("invalid --host or --port")?;
    let listener = tokio::net::TcpListener::bind(address).await.with_context(|| format!("cannot listen on {address}"))?;
    eprintln!("Hermes speech service listening on {address}");
    axum::serve(listener, router(app)).with_graceful_shutdown(shutdown()).await?;
    Ok(())
}

/// The token comes from the environment, or from the Hermes profile's
/// `.env` without overriding exported variables.
fn load_token(name: &str) -> anyhow::Result<String> {
    let mut token = std::env::var(name).ok();
    if token.is_none() {
        let home = std::env::var_os("HERMES_HOME")
            .map(PathBuf::from)
            .or_else(|| std::env::home_dir().map(|h| h.join(".hermes")));
        if let Some(path) = home.map(|h| h.join(".env")).filter(|p| p.is_file()) {
            for item in dotenvy::from_path_iter(&path)? {
                let (key, value) = item.context("cannot read the Hermes .env file")?;
                if key == name {
                    token = Some(value);
                }
            }
        }
    }
    match token {
        Some(t) if t.len() >= 16 && t.bytes().all(|b| (33..=126).contains(&b)) => Ok(t),
        Some(_) => bail!("{name} must be a printable token of at least 16 characters"),
        None => bail!("set the service token in {name}"),
    }
}

async fn shutdown() {
    let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("signal handler");
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {}
        _ = term.recv() => {}
    }
}

fn router(app: Arc<App>) -> Router {
    Router::new()
        .route("/v1/speech", post(speak))
        .fallback(|| async { (StatusCode::NOT_FOUND, "unknown endpoint") })
        .layer(DefaultBodyLimit::max(MAX_REQUEST_BYTES))
        .with_state(app)
}

/// Compare without leaking how much of the token matched.
fn same(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

fn plain(status: StatusCode, body: &'static str) -> Response {
    (status, [(header::CACHE_CONTROL, "no-store")], body).into_response()
}

async fn speak(State(app): State<Arc<App>>, headers: HeaderMap, body: Bytes) -> Response {
    let expected = format!("Bearer {}", app.token);
    let given = headers.get(header::AUTHORIZATION).map(|v| v.as_bytes()).unwrap_or(b"");
    if !same(given, expected.as_bytes()) {
        return plain(StatusCode::UNAUTHORIZED, "service token required");
    }
    let json = headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.split(';').next().unwrap_or("").trim() == "application/json");
    let request = match serde_json::from_slice::<Request>(&body) {
        Ok(request) if json => request,
        _ => return plain(StatusCode::BAD_REQUEST, "invalid speech request"),
    };
    if request.validate(app.limits.max_audio).is_err() {
        return plain(StatusCode::BAD_REQUEST, "invalid speech request or limits");
    }
    let Ok(_permit) = app.busy.try_acquire() else {
        return plain(StatusCode::SERVICE_UNAVAILABLE, "speech service busy; try again");
    };
    match speech::generate(&request, &app.limits).await {
        Ok(wav) => (StatusCode::OK, [(header::CONTENT_TYPE, "audio/wav"), (header::CACHE_CONTROL, "no-store")], wav).into_response(),
        Err(SpeechError::TimedOut) => plain(StatusCode::GATEWAY_TIMEOUT, "speech generation timed out"),
        Err(SpeechError::TooLong) => plain(StatusCode::PAYLOAD_TOO_LARGE, "speech is longer than requested"),
        Err(SpeechError::Failed(reason)) => {
            // Never log the text or provider output.
            eprintln!("speech request failed: {reason}");
            plain(StatusCode::BAD_GATEWAY, "the configured Hermes speech provider failed; no fallback")
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    const TOKEN: &str = "0123456789abcdef-token";

    /// A stand-in for Hermes's Python: makes a short tone with ffmpeg.
    fn fake_python(dir: &std::path::Path, seconds: f64) -> PathBuf {
        let path = dir.join("python");
        let script = format!(
            "#!/bin/sh\n# args: -c SNIPPET ROOT REQUEST OUTDIR\nout=\"$5/source.wav\"\nffmpeg -nostdin -v error -f lavfi -i sine=frequency=440:duration={seconds} -ar 22050 \"$out\" && echo \"{{\\\"file\\\": \\\"$out\\\"}}\"\n"
        );
        std::fs::write(&path, script).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        path
    }

    async fn serve(seconds: f64) -> (tempfile::TempDir, String) {
        let dir = tempfile::tempdir().unwrap();
        let app = Arc::new(App {
            token: TOKEN.into(),
            limits: Limits {
                hermes_root: dir.path().to_path_buf(),
                python: fake_python(dir.path(), seconds),
                max_audio: 120.0,
                timeout: Duration::from_secs(20),
            },
            busy: Semaphore::new(1),
        });
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/v1/speech", listener.local_addr().unwrap());
        tokio::spawn(async move { axum::serve(listener, router(app)).await.unwrap() });
        (dir, url)
    }

    fn post(url: &str, token: &str, body: serde_json::Value) -> reqwest::RequestBuilder {
        reqwest::Client::new().post(url).bearer_auth(token).json(&body)
    }

    #[tokio::test]
    async fn returns_radio_ready_wav() {
        let (_dir, url) = serve(1.0).await;
        let response = post(&url, TOKEN, serde_json::json!({"text": "Hello.", "max_seconds": 5.0, "crop": true})).send().await.unwrap();
        assert_eq!(response.status(), 200);
        let bytes = response.bytes().await.unwrap();
        let reader = hound::WavReader::new(std::io::Cursor::new(bytes.to_vec())).unwrap();
        let spec = reader.spec();
        assert_eq!((spec.channels, spec.sample_rate, spec.bits_per_sample), (1, 48_000, 16));
        assert!((reader.duration() as i64 - 48_000).abs() < 2_000);
    }

    #[tokio::test]
    async fn strict_requests_refuse_long_speech_and_crop_requests_cut_it() {
        let (_dir, url) = serve(3.0).await;
        let strict = post(&url, TOKEN, serde_json::json!({"text": "A callsign.", "max_seconds": 1.0, "crop": false})).send().await.unwrap();
        assert_eq!(strict.status(), 413);
        let cropped = post(&url, TOKEN, serde_json::json!({"text": "A reply.", "max_seconds": 1.0, "crop": true})).send().await.unwrap();
        assert_eq!(cropped.status(), 200);
        let bytes = cropped.bytes().await.unwrap();
        let reader = hound::WavReader::new(std::io::Cursor::new(bytes.to_vec())).unwrap();
        assert!(reader.duration() <= 48_000);
    }

    #[tokio::test]
    async fn rejects_bad_tokens_and_invalid_requests() {
        let (_dir, url) = serve(1.0).await;
        let wrong = post(&url, "wrong-token-wrong-token", serde_json::json!({"text": "x", "max_seconds": 1.0, "crop": true})).send().await.unwrap();
        assert_eq!(wrong.status(), 401);
        for body in [
            serde_json::json!({"text": "", "max_seconds": 1.0, "crop": true}),
            serde_json::json!({"text": "x", "max_seconds": 0.0, "crop": true}),
            serde_json::json!({"text": "x", "max_seconds": 500.0, "crop": true}),
            serde_json::json!({"text": "bell\u{7}", "max_seconds": 1.0, "crop": true}),
            serde_json::json!({"text": "x", "max_seconds": 1.0, "crop": true, "extra": 1}),
        ] {
            let response = post(&url, TOKEN, body.clone()).send().await.unwrap();
            assert_eq!(response.status(), 400, "{body}");
        }
    }
}
