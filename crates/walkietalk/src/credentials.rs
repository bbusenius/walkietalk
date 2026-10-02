//! Private tokens and API keys.
//!
//! Secrets live in `credentials.toml` beside the config, never in the config
//! itself. Variables already exported in the environment take precedence.
//! The file must be a regular file owned by the current user and unreadable
//! by anyone else. Error messages never include its contents.

use std::collections::BTreeMap;
use std::fmt;
use std::fs::OpenOptions;
use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};

use anyhow::{Context, bail};

const MAX_FILE_BYTES: u64 = 64 * 1024;
pub const FILE_NAME: &str = "credentials.toml";

/// A secret value. It never appears in `Debug` output or error messages.
#[derive(Clone, PartialEq, Eq)]
pub struct Secret(String);

impl Secret {
    pub fn new(value: impl Into<String>) -> Secret {
        Secret(value.into())
    }

    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl fmt::Debug for Secret {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Secret(<redacted>)")
    }
}

/// Where a secret was found.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Source {
    Environment,
    File,
}

/// Secrets from the environment and the optional credentials file.
#[derive(Debug, Default, Clone)]
pub struct Credentials {
    file: BTreeMap<String, Secret>,
    path: Option<PathBuf>,
    /// Lookups go through this function so tests never touch the real environment.
    env: Option<fn(&str) -> Option<String>>,
}

impl Credentials {
    /// Only the process environment.
    pub fn environment_only() -> Credentials {
        Credentials::default()
    }

    /// Load `path`. A missing file is an error only when `required`.
    pub fn load(path: &Path, required: bool) -> anyhow::Result<Credentials> {
        let file = match OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(path)
        {
            Ok(file) => file,
            Err(err) if err.kind() == std::io::ErrorKind::NotFound && !required => {
                return Ok(Credentials::default());
            }
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
                bail!("credentials file not found: {}", path.display())
            }
            Err(_) => bail!(
                "cannot open credentials file {}; it must be a regular file, not a symlink",
                path.display()
            ),
        };
        let meta = file.metadata().context("cannot inspect credentials file")?;
        if !meta.file_type().is_file()
            || meta.uid() != crate::sys::current_uid()
            || meta.mode() & 0o077 != 0
        {
            bail!(
                "credentials file {} must be a regular file owned by you with no group or other access (chmod 600)",
                path.display()
            );
        }
        if meta.len() > MAX_FILE_BYTES {
            bail!(
                "credentials file {} exceeds 64 KiB; contents withheld",
                path.display()
            );
        }
        let mut text = String::new();
        file.take(MAX_FILE_BYTES + 1)
            .read_to_string(&mut text)
            .map_err(|_| {
                anyhow::anyhow!("credentials file {} must be UTF-8 text", path.display())
            })?;
        let file = parse(&text).with_context(|| format!("credentials file {}", path.display()))?;
        Ok(Credentials {
            file,
            path: Some(path.to_path_buf()),
            env: None,
        })
    }

    #[cfg(test)]
    pub fn for_tests(values: &[(&str, &str)], env: fn(&str) -> Option<String>) -> Credentials {
        Credentials {
            file: values
                .iter()
                .map(|(k, v)| (k.to_string(), Secret::new(*v)))
                .collect(),
            path: None,
            env: Some(env),
        }
    }

    pub fn path(&self) -> Option<&Path> {
        self.path.as_deref()
    }

    /// Look up a variable. An exported variable wins, even when empty.
    pub fn get(&self, name: &str) -> Option<(Secret, Source)> {
        let env = match self.env {
            Some(lookup) => lookup(name),
            None => std::env::var(name).ok(),
        };
        match env {
            Some(value) => Some((Secret(value), Source::Environment)),
            None => self.file.get(name).map(|s| (s.clone(), Source::File)),
        }
    }

    /// Whether a usable value exists, for readiness reports.
    pub fn has(&self, name: &str) -> bool {
        self.token(name).is_ok()
    }

    /// A bearer token or API key: non-empty printable ASCII without spaces.
    pub fn token(&self, name: &str) -> anyhow::Result<Secret> {
        match self.get(name) {
            None => bail!("{name} is not set; add it to {FILE_NAME} or the environment"),
            Some((secret, source)) if secret.0.is_empty() => match source {
                Source::Environment => {
                    bail!("{name} is exported but empty; unset it or give it a value")
                }
                Source::File => bail!("{name} is empty in {FILE_NAME}"),
            },
            Some((secret, _)) if !secret.0.bytes().all(|b| (33..=126).contains(&b)) => {
                bail!("{name} must be a token without spaces or control characters")
            }
            Some((secret, _)) => Ok(secret),
        }
    }
}

fn parse(text: &str) -> anyhow::Result<BTreeMap<String, Secret>> {
    let table: toml::Table = text.parse().map_err(|err: toml::de::Error| {
        let line = err
            .span()
            .map(|span| text[..span.start].matches('\n').count() + 1)
            .map_or(String::new(), |line| format!(" at line {line}"));
        anyhow::anyhow!("invalid TOML{line}; contents withheld")
    })?;
    let mut values = BTreeMap::new();
    for (key, value) in table {
        let valid_name = key
            .chars()
            .next()
            .is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
            && key.chars().all(|c| c.is_ascii_alphanumeric() || c == '_');
        if !valid_name {
            bail!("entries must be VARIABLE_NAME = \"value\"; contents withheld");
        }
        match value {
            toml::Value::String(s) if !s.contains('\0') => {
                values.insert(key, Secret(s));
            }
            _ => bail!("{key} must be a quoted string"),
        }
    }
    Ok(values)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn no_env(_: &str) -> Option<String> {
        None
    }

    fn env_has_empty_token(name: &str) -> Option<String> {
        (name == "TOKEN").then(String::new)
    }

    fn write(dir: &Path, text: &str, mode: u32) -> PathBuf {
        let path = dir.join(FILE_NAME);
        std::fs::write(&path, text).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode)).unwrap();
        path
    }

    #[test]
    fn private_file_loads() {
        let dir = tempfile::tempdir().unwrap();
        let path = write(
            dir.path(),
            "TOKEN = \"abc123\"\n# comment\nOTHER = \"\"\n",
            0o600,
        );
        let creds = Credentials::load(&path, true).unwrap();
        assert_eq!(creds.file["TOKEN"].expose(), "abc123");
    }

    #[test]
    fn readable_file_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        let path = write(dir.path(), "TOKEN = \"abc\"\n", 0o644);
        let err = Credentials::load(&path, true).unwrap_err().to_string();
        assert!(err.contains("chmod 600"), "{err}");
    }

    #[test]
    fn symlink_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        let real = write(dir.path(), "TOKEN = \"abc\"\n", 0o600);
        let link = dir.path().join("link.toml");
        std::os::unix::fs::symlink(&real, &link).unwrap();
        assert!(Credentials::load(&link, true).is_err());
    }

    #[test]
    fn missing_optional_file_is_fine_but_required_is_not() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join(FILE_NAME);
        assert!(Credentials::load(&path, false).is_ok());
        assert!(Credentials::load(&path, true).is_err());
    }

    #[test]
    fn errors_never_contain_secret_values() {
        let dir = tempfile::tempdir().unwrap();
        for text in [
            "TOKEN = \"sk-supersecret\"\nTOKEN = \"sk-supersecret\"\n",
            "TOKEN = sk-supersecret\n",
            "TOKEN = [\"sk-supersecret\"]\n",
            "bad name = \"sk-supersecret\"\n",
        ] {
            let path = write(dir.path(), text, 0o600);
            let err = format!("{:#}", Credentials::load(&path, true).unwrap_err());
            assert!(!err.contains("supersecret"), "{err}");
        }
    }

    #[test]
    fn environment_wins_even_when_empty() {
        let creds = Credentials::for_tests(&[("TOKEN", "from-file")], env_has_empty_token);
        let err = creds.token("TOKEN").unwrap_err().to_string();
        assert!(err.contains("exported but empty"), "{err}");
        let creds = Credentials::for_tests(&[("TOKEN", "from-file")], no_env);
        assert_eq!(creds.token("TOKEN").unwrap().expose(), "from-file");
    }

    #[test]
    fn debug_output_redacts_secrets() {
        let creds = Credentials::for_tests(&[("TOKEN", "sk-supersecret")], no_env);
        assert!(!format!("{creds:?}").contains("supersecret"));
    }

    #[test]
    fn tokens_with_spaces_are_rejected() {
        let creds = Credentials::for_tests(&[("TOKEN", "two words")], no_env);
        assert!(creds.token("TOKEN").is_err());
    }
}
