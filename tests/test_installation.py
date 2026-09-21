import os
import stat
from importlib.resources import files
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from walkietalk import cli
from walkietalk.config import WalkietalkError, load_config
from walkietalk.setup import credentials_environment, initialize, read_credentials


def private_file(path, contents):
    path.write_text(contents)
    path.chmod(0o600)
    return path


def test_init_from_packaged_resources_is_complete_private_and_never_overwrites(tmp_path):
    directory = tmp_path / "settings"
    initialize(directory)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for name in ("config.yaml", "credentials.env"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    config = load_config(directory / "config.yaml")
    assert config.agent_backend == "stub" and config.line == "dtr"
    assert read_credentials(directory / "credentials.env", required=True) == {}
    private_file(directory / "credentials.env", "TOKEN=my-existing-private-token")
    with pytest.raises(WalkietalkError, match="nothing overwritten"):
        initialize(directory)
    assert (directory / "credentials.env").read_text() == "TOKEN=my-existing-private-token"


def test_packaged_example_matches_documented_schema():
    template = files("walkietalk").joinpath("data/config.example.yaml").read_text()
    assert template == Path("config.example.yaml").read_text()


def test_automatic_credentials_are_config_adjacent_and_environment_wins(tmp_path, monkeypatch):
    directory = tmp_path / "selected"
    directory.mkdir()
    (directory / "config.yaml").write_text("selected")
    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    private_file(
        directory / "credentials.env",
        'NEW_KEY="literal${NO_EXPANSION}#value"\nEXISTING=file\nEMPTY=file',
    )
    private_file(elsewhere / "credentials.env", "WRONG_DIRECTORY=should-not-be-loaded")
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("EXISTING", "process")
    monkeypatch.setenv("EMPTY", "")
    with credentials_environment(directory / "config.yaml", None):
        assert os.environ["NEW_KEY"] == "literal${NO_EXPANSION}#value"
        assert os.environ["EXISTING"] == "process" and os.environ["EMPTY"] == ""
        assert "WRONG_DIRECTORY" not in os.environ
    assert "NEW_KEY" not in os.environ


def test_explicit_file_no_file_mode_and_cleanup_on_error(tmp_path):
    path = private_file(tmp_path / "custom.env", "SETUP_TOKEN=test-value")
    with pytest.raises(RuntimeError):
        with credentials_environment(None, path):
            assert os.environ["SETUP_TOKEN"] == "test-value"
            raise RuntimeError
    assert "SETUP_TOKEN" not in os.environ
    with credentials_environment(None, path, disabled=True):
        assert "SETUP_TOKEN" not in os.environ
    with pytest.raises(WalkietalkError, match="not found"):
        read_credentials(tmp_path / "missing", required=True)
    assert read_credentials(tmp_path / "missing", required=False) == {}


@pytest.mark.parametrize("kind", ["public", "symlink", "fifo", "directory", "oversized"])
def test_unsafe_credential_files_fail_locally(tmp_path, kind):
    path = tmp_path / "credentials.env"
    if kind == "symlink":
        target = private_file(tmp_path / "real", "TOKEN=do-not-print")
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "directory":
        path.mkdir(mode=0o700)
    else:
        private_file(path, "TOKEN=" + ("x" * 65536 if kind == "oversized" else "do-not-print"))
        if kind == "public":
            path.chmod(0o644)
    with pytest.raises(WalkietalkError) as error:
        read_credentials(path, required=True)
    assert "do-not-print" not in str(error.value)


@pytest.mark.parametrize(
    "contents",
    [
        'TOKEN="unterminated-secret',
        "TOKEN=unquoted secret",
        "NOT AN ASSIGNMENT",
        "TOKEN=one\nTOKEN=two",
        "TOKEN=bad\x00value",
    ],
)
def test_invalid_credentials_do_not_leak_or_partially_apply(tmp_path, contents):
    path = private_file(tmp_path / "credentials.env", "SETUP_TEST_TOKEN=private\n" + contents)
    with pytest.raises(WalkietalkError, match="line") as error:
        with credentials_environment(None, path):
            pytest.fail("Invalid credentials accepted")
    assert "SETUP_TEST_TOKEN" not in os.environ
    assert "secret" not in str(error.value) and "private" not in str(error.value)


def test_file_values_are_not_executed(tmp_path):
    marker = tmp_path / "must-not-exist"
    path = private_file(tmp_path / "credentials.env", f'TOKEN="$(touch {marker})"')
    assert read_credentials(path, required=True)["TOKEN"] == f"$(touch {marker})"
    assert not marker.exists()


def test_config_check_and_stub_need_no_hardware_network_or_account(tmp_path, monkeypatch, capsys):
    initialize(tmp_path / "settings")
    config = tmp_path / "settings/config.yaml"
    for name in (
        "preflight",
        "audio_devices",
        "open_stt",
        "open_tts",
        "SerialPTT",
        "Playback",
        "transmit",
    ):
        monkeypatch.setattr(
            cli, name, Mock(side_effect=AssertionError("Hardware/network requested"))
        )
    assert cli.main(["-c", str(config), "config-check"]) == 0
    assert "Config OK" in capsys.readouterr().out
    assert cli.main(["-c", str(config), "agent-check", "Hello"]) == 0
    assert "Reply:" in capsys.readouterr().out
    data = yaml.safe_load(config.read_text())
    data["agent"]["backend"] = "hermes"
    config.write_text(yaml.safe_dump(data))
    assert cli.main(["-c", str(config), "config-check"]) == 0
    assert "Agent: hermes" in capsys.readouterr().out


def test_cli_loads_token_before_agent_and_restores_environment(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("selected")
    private_file(tmp_path / "credentials.env", "INSTALL_TEST_TOKEN=private")

    def run(args):
        assert os.environ["INSTALL_TEST_TOKEN"] == "private"

    monkeypatch.setattr(cli, "run", run)
    assert cli.main(["-c", str(config), "agent-check"]) == 0
    assert "INSTALL_TEST_TOKEN" not in os.environ


def test_missing_config_does_not_read_adjacent_credentials(tmp_path, monkeypatch, capsys):
    private_file(tmp_path / "credentials.env", "TOKEN=do-not-print")
    (tmp_path / "credentials.env").chmod(0o644)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["-c", "missing.yaml", "config-check"]) == 1
    error = capsys.readouterr().err
    assert "Cannot read config" in error
    assert "do-not-print" not in error
    assert "TOKEN" not in os.environ


def test_version_does_not_read_credentials_or_touch_hardware(capsys):
    with pytest.raises(SystemExit) as result:
        cli.main(["--env-file", "/not/a/file", "--version"])
    assert result.value.code == 0
    assert capsys.readouterr().out.strip() == "walkietalk 0.1.0"
