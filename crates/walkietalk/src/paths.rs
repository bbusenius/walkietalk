//! Standard per-user locations.

use std::env;
use std::path::{Path, PathBuf};

pub fn home() -> PathBuf {
    env::home_dir().unwrap_or_else(|| PathBuf::from("/"))
}

/// Replace a leading `~` with the home directory.
pub fn expand_home(path: &Path) -> PathBuf {
    match path.strip_prefix("~") {
        Ok(rest) => home().join(rest),
        Err(_) => path.to_path_buf(),
    }
}

fn xdg(var: &str, fallback: &str) -> PathBuf {
    env::var_os(var)
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .unwrap_or_else(|| home().join(fallback))
}

pub fn config_home() -> PathBuf {
    xdg("XDG_CONFIG_HOME", ".config")
}

pub fn data_home() -> PathBuf {
    xdg("XDG_DATA_HOME", ".local/share")
}

pub fn state_home() -> PathBuf {
    xdg("XDG_STATE_HOME", ".local/state")
}

pub fn cache_home() -> PathBuf {
    xdg("XDG_CACHE_HOME", ".cache")
}

/// Default settings directory created by `walkietalk init`.
pub fn default_settings_dir() -> PathBuf {
    config_home().join("walkietalk")
}

pub fn default_config_file() -> PathBuf {
    default_settings_dir().join("config.toml")
}

/// Where downloaded speech models live.
pub fn models_dir() -> PathBuf {
    data_home().join("walkietalk").join("models")
}
