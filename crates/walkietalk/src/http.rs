//! HTTP helpers: one client policy and bounded response bodies.

use std::time::Duration;

use anyhow::bail;
use futures_util::StreamExt;

/// A client that never follows redirects or uses ambient proxies, so a
/// bearer token only ever goes to the configured host.
pub fn client(connect_timeout: Duration) -> reqwest::Client {
    reqwest::Client::builder()
        .connect_timeout(connect_timeout)
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .build()
        .expect("HTTP client configuration is valid")
}

/// Read a body, refusing more than `max` bytes.
pub async fn body(response: reqwest::Response, max: usize, what: &str) -> anyhow::Result<Vec<u8>> {
    if response
        .content_length()
        .is_some_and(|len| len > max as u64)
    {
        bail!("{what} response is larger than {max} bytes; discarded");
    }
    let mut data = Vec::new();
    let mut stream = response.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk
            .map_err(|err| anyhow::anyhow!("{what} response interrupted: {}", without_url(&err)))?;
        if data.len() + chunk.len() > max {
            bail!("{what} response is larger than {max} bytes; discarded");
        }
        data.extend_from_slice(&chunk);
    }
    Ok(data)
}

/// Parse a bounded JSON body.
pub async fn json(
    response: reqwest::Response,
    max: usize,
    what: &str,
) -> anyhow::Result<serde_json::Value> {
    let data = body(response, max, what).await?;
    serde_json::from_slice(&data).map_err(|_| anyhow::anyhow!("{what} returned malformed JSON"))
}

/// Describe a transport error without echoing URLs (which can carry tokens).
pub fn without_url(err: &reqwest::Error) -> String {
    let kind = if err.is_timeout() {
        "timed out"
    } else if err.is_connect() {
        "connection failed"
    } else if err.is_request() {
        "request failed"
    } else if err.is_body() || err.is_decode() {
        "invalid response body"
    } else {
        "transport error"
    };
    kind.to_string()
}

/// Run `work` with an overall deadline covering connect, headers, and body.
pub async fn within<T>(
    timeout: Duration,
    what: &str,
    work: impl Future<Output = anyhow::Result<T>>,
) -> anyhow::Result<T> {
    match tokio::time::timeout(timeout, work).await {
        Ok(result) => result,
        Err(_) => bail!("{what} timed out after {:.0}s", timeout.as_secs_f64()),
    }
}
