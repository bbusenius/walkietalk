//! `walkietalk init`: a new private settings directory.

use std::fs::{DirBuilder, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::path::Path;

use anyhow::{Context, bail};

pub const CONFIG_TEMPLATE: &str = include_str!("../templates/config.toml");
pub const CREDENTIALS_TEMPLATE: &str = include_str!("../templates/credentials.toml");

/// Create `dir` with a config and a private credentials file. Refuses to
/// touch an existing directory, even an empty one.
pub fn init(dir: &Path) -> anyhow::Result<()> {
    if let Some(parent) = dir.parent().filter(|p| !p.as_os_str().is_empty()) {
        DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(parent)
            .with_context(|| format!("cannot create {}", parent.display()))?;
    }
    match DirBuilder::new().mode(0o700).create(dir) {
        Ok(()) => {}
        Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => bail!(
            "{} already exists; nothing was changed. Edit your existing settings or choose a new --directory",
            dir.display()
        ),
        Err(err) => return Err(err).with_context(|| format!("cannot create {}", dir.display())),
    }
    for (name, contents) in [
        ("config.toml", CONFIG_TEMPLATE),
        (crate::credentials::FILE_NAME, CREDENTIALS_TEMPLATE),
    ] {
        let path = dir.join(name);
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&path)
            .with_context(|| format!("cannot create {}", path.display()))?;
        file.write_all(contents.as_bytes())?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn creates_private_files_and_never_overwrites() {
        let root = tempfile::tempdir().unwrap();
        let dir = root.path().join("nested/walkietalk");
        init(&dir).unwrap();
        for name in ["config.toml", "credentials.toml"] {
            let mode = std::fs::metadata(dir.join(name))
                .unwrap()
                .permissions()
                .mode();
            assert_eq!(mode & 0o777, 0o600, "{name}");
        }
        let mode = std::fs::metadata(&dir).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o700);
        std::fs::write(dir.join("config.toml"), "mine").unwrap();
        assert!(init(&dir).is_err());
        assert_eq!(
            std::fs::read_to_string(dir.join("config.toml")).unwrap(),
            "mine"
        );
    }

    #[test]
    fn existing_empty_directory_is_refused() {
        let root = tempfile::tempdir().unwrap();
        assert!(init(root.path()).is_err());
    }

    #[test]
    fn credentials_template_loads() {
        let root = tempfile::tempdir().unwrap();
        let dir = root.path().join("w");
        init(&dir).unwrap();
        crate::credentials::Credentials::load(&dir.join("credentials.toml"), true).unwrap();
    }
}
