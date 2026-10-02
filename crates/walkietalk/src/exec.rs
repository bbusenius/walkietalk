//! Bounded child processes.
//!
//! Every external program runs in its own process group with a deadline
//! and a cap on how much output is kept. Whatever happens (success,
//! failure, timeout, or the caller giving up) the whole group is killed, so
//! helpers started by the program cannot outlive it.

use std::collections::HashMap;
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::{ExitStatus, Stdio};
use std::time::Duration;

use tokio::io::{AsyncRead, AsyncReadExt, AsyncWriteExt};
use tokio::process::{Child, Command};

#[derive(Debug, thiserror::Error)]
pub enum ExecError {
    #[error("{program} not found; install it or set its executable path in the config")]
    NotFound { program: String },
    #[error("cannot start {program}: {source}")]
    Spawn { program: String, source: std::io::Error },
    #[error("{program} timed out after {:.0}s", .after.as_secs_f64())]
    Timeout { program: String, after: Duration },
    #[error("{program} produced more output than allowed; discarded")]
    TooLarge { program: String },
    #[error("{program} I/O failed: {source}")]
    Io { program: String, source: std::io::Error },
}

#[derive(Debug)]
pub struct Output {
    pub status: ExitStatus,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}

impl Output {
    pub fn success(&self) -> bool {
        self.status.success()
    }
}

/// What to run and its limits.
pub struct Job {
    program: PathBuf,
    label: String,
    args: Vec<OsString>,
    env: HashMap<String, String>,
    cwd: Option<PathBuf>,
    stdin: Option<Vec<u8>>,
    timeout: Duration,
    max_output: usize,
}

/// Locate `name` on `PATH` (or accept an absolute path).
pub fn find(name: &str) -> Result<PathBuf, ExecError> {
    let found = if Path::new(name).is_absolute() {
        Path::new(name).is_file().then(|| PathBuf::from(name))
    } else {
        which::which(name).ok()
    };
    found.ok_or_else(|| ExecError::NotFound { program: name.to_string() })
}

/// Variables passed through from our environment when present.
pub fn inherit(names: &[&str]) -> HashMap<String, String> {
    names
        .iter()
        .filter_map(|name| std::env::var(name).ok().map(|v| (name.to_string(), v)))
        .collect()
}

impl Job {
    /// `label` names the program in errors.
    pub fn new(program: PathBuf, label: &str) -> Job {
        Job {
            program,
            label: label.to_string(),
            args: Vec::new(),
            env: HashMap::new(),
            cwd: None,
            stdin: None,
            timeout: Duration::from_secs(30),
            max_output: 1024 * 1024,
        }
    }

    pub fn arg(mut self, arg: impl Into<OsString>) -> Job {
        self.args.push(arg.into());
        self
    }

    pub fn args<I, S>(mut self, args: I) -> Job
    where
        I: IntoIterator<Item = S>,
        S: Into<OsString>,
    {
        self.args.extend(args.into_iter().map(Into::into));
        self
    }

    /// The child's entire environment; nothing else is inherited.
    pub fn env(mut self, env: HashMap<String, String>) -> Job {
        self.env = env;
        self
    }

    pub fn set_env(mut self, key: &str, value: &str) -> Job {
        self.env.insert(key.to_string(), value.to_string());
        self
    }

    pub fn cwd(mut self, dir: &Path) -> Job {
        self.cwd = Some(dir.to_path_buf());
        self
    }

    pub fn stdin(mut self, bytes: Vec<u8>) -> Job {
        self.stdin = Some(bytes);
        self
    }

    pub fn timeout(mut self, timeout: Duration) -> Job {
        self.timeout = timeout;
        self
    }

    /// Cap on stdout plus stderr.
    pub fn max_output(mut self, bytes: usize) -> Job {
        self.max_output = bytes;
        self
    }

    pub fn command(&self) -> Command {
        let mut command = Command::new(&self.program);
        command
            .args(&self.args)
            .env_clear()
            .envs(&self.env)
            .process_group(0)
            .kill_on_drop(true);
        if let Some(dir) = &self.cwd {
            command.current_dir(dir);
        }
        command
    }

    /// Run to completion within the limits.
    pub async fn run(self) -> Result<Output, ExecError> {
        let label = self.label.clone();
        let mut command = self.command();
        command
            .stdin(if self.stdin.is_some() { Stdio::piped() } else { Stdio::null() })
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let mut child = command.spawn().map_err(|source| spawn_error(&label, source))?;
        let group = Group::of(&child);
        let work = collect(&mut child, self.stdin, self.max_output, &label);
        let result = match tokio::time::timeout(self.timeout, work).await {
            Ok(result) => result,
            Err(_) => Err(ExecError::Timeout {
                program: label,
                after: self.timeout,
            }),
        };
        group.terminate().await;
        result
    }
}

fn spawn_error(label: &str, source: std::io::Error) -> ExecError {
    if source.kind() == std::io::ErrorKind::NotFound {
        ExecError::NotFound { program: label.to_string() }
    } else {
        ExecError::Spawn { program: label.to_string(), source }
    }
}

async fn collect(child: &mut Child, stdin: Option<Vec<u8>>, max: usize, label: &str) -> Result<Output, ExecError> {
    let io = |source| ExecError::Io { program: label.to_string(), source };
    if let (Some(bytes), Some(mut pipe)) = (stdin, child.stdin.take()) {
        // A program that never reads its input must not block us, and one
        // that exits early is not an error here.
        tokio::spawn(async move {
            let _ = pipe.write_all(&bytes).await;
            let _ = pipe.shutdown().await;
        });
    }
    let stdout = child.stdout.take().expect("stdout is piped");
    let stderr = child.stderr.take().expect("stderr is piped");
    let budget = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(max));
    let (out, err) = tokio::join!(read_capped(stdout, budget.clone()), read_capped(stderr, budget));
    let too_large = || ExecError::TooLarge { program: label.to_string() };
    let stdout = out.map_err(io)?.ok_or_else(too_large)?;
    let stderr = err.map_err(io)?.ok_or_else(too_large)?;
    let status = child.wait().await.map_err(io)?;
    Ok(Output { status, stdout, stderr })
}

/// Read to the end, or `None` once the shared budget is exhausted.
async fn read_capped(mut pipe: impl AsyncRead + Unpin, budget: std::sync::Arc<std::sync::atomic::AtomicUsize>) -> std::io::Result<Option<Vec<u8>>> {
    use std::sync::atomic::Ordering;
    let mut data = Vec::new();
    let mut buf = [0u8; 8192];
    loop {
        let n = pipe.read(&mut buf).await?;
        if n == 0 {
            return Ok(Some(data));
        }
        let ok = budget
            .try_update(Ordering::SeqCst, Ordering::SeqCst, |left| left.checked_sub(n))
            .is_ok();
        if !ok {
            return Ok(None);
        }
        data.extend_from_slice(&buf[..n]);
    }
}

/// A child's process group, killed on request or when dropped.
pub struct Group {
    pgid: Option<i32>,
}

impl Group {
    pub fn of(child: &Child) -> Group {
        Group {
            pgid: child.id().map(|id| id as i32),
        }
    }

    fn signal(&self, signal: i32) {
        if let Some(pgid) = self.pgid {
            // SAFETY: killpg only sends a signal; a stale group ID is harmless (ESRCH).
            unsafe {
                libc::killpg(pgid, signal);
            }
        }
    }

    /// Ask the group to stop, then kill whatever remains.
    pub async fn terminate(mut self) {
        self.signal(libc::SIGTERM);
        tokio::time::sleep(Duration::from_millis(200)).await;
        self.signal(libc::SIGKILL);
        self.pgid = None;
    }
}

impl Drop for Group {
    fn drop(&mut self) {
        self.signal(libc::SIGKILL);
    }
}

/// A long-running child (a sync daemon, for example) whose group is killed
/// when this is dropped.
pub struct Daemon {
    pub child: Child,
    group: Group,
}

impl Daemon {
    pub fn spawn(mut command: Command, label: &str) -> Result<Daemon, ExecError> {
        let child = command.spawn().map_err(|source| spawn_error(label, source))?;
        let group = Group::of(&child);
        Ok(Daemon { child, group })
    }

    pub async fn stop(self) {
        let Daemon { mut child, group } = self;
        group.terminate().await;
        let _ = tokio::time::timeout(Duration::from_secs(1), child.wait()).await;
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    /// Write an executable shell script and return its path.
    pub fn script(dir: &Path, name: &str, body: &str) -> PathBuf {
        let path = dir.join(name);
        std::fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        path
    }

    fn job(path: PathBuf) -> Job {
        Job::new(path, "fake").env(inherit(&["PATH"]))
    }

    #[tokio::test]
    async fn collects_output_and_status() {
        let dir = tempfile::tempdir().unwrap();
        let path = script(dir.path(), "echo", "read line; echo \"got $line\"; echo oops >&2; exit 3");
        let out = job(path).stdin(b"hello\n".to_vec()).run().await.unwrap();
        assert_eq!(out.stdout, b"got hello\n");
        assert_eq!(out.stderr, b"oops\n");
        assert_eq!(out.status.code(), Some(3));
    }

    #[tokio::test]
    async fn timeout_kills_the_whole_group() {
        let dir = tempfile::tempdir().unwrap();
        let marker = dir.path().join("survived");
        let body = format!("(sleep 1; touch {}) &\nsleep 30", marker.display());
        let path = script(dir.path(), "hang", &body);
        let started = std::time::Instant::now();
        let err = job(path).timeout(Duration::from_millis(200)).run().await.unwrap_err();
        assert!(matches!(err, ExecError::Timeout { .. }));
        assert!(started.elapsed() < Duration::from_secs(2));
        tokio::time::sleep(Duration::from_millis(1300)).await;
        assert!(!marker.exists(), "a background helper outlived the timeout");
    }

    #[tokio::test]
    async fn output_cap_is_enforced() {
        let dir = tempfile::tempdir().unwrap();
        let path = script(dir.path(), "flood", "head -c 100000 /dev/zero");
        let err = job(path).max_output(1000).run().await.unwrap_err();
        assert!(matches!(err, ExecError::TooLarge { .. }));
    }

    #[tokio::test]
    async fn environment_is_only_what_was_given() {
        let dir = tempfile::tempdir().unwrap();
        let path = script(dir.path(), "env", "env");
        let out = job(path).set_env("ONLY_THIS", "1").run().await.unwrap();
        let text = String::from_utf8(out.stdout).unwrap();
        assert!(text.contains("ONLY_THIS=1"));
        assert!(!text.contains("HOME="));
    }

    #[tokio::test]
    async fn missing_program_is_reported() {
        let err = job(PathBuf::from("/nonexistent/prog")).run().await.unwrap_err();
        assert!(matches!(err, ExecError::NotFound { .. }));
        assert!(matches!(find("definitely-not-a-real-program-xyz"), Err(ExecError::NotFound { .. })));
    }
}
