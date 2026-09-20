import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from walkietalk import cli
from walkietalk.agent import AgentSession, open_agent
from walkietalk.claude import REQUIRED_FLAGS, ClaudeCodeAgent
from walkietalk.config import Config, WalkietalkError, load_config

SECRET = "fake-private-cli-diagnostic"
AUTH = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"}


@pytest.fixture
def config():
    return replace(Config(), agent_backend="claude")


@pytest.fixture(autouse=True)
def no_other_backend_or_hardware(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Claude Code touched another backend or radio hardware")

    for name in ("SerialPTT", "DryPTT", "Playback", "transmit", "preflight", "open_stt"):
        monkeypatch.setattr(cli, name, forbidden)
    for module, name in (
        ("agent", "StubAgent"),
        ("hermes", "HermesAgent"),
        ("codex", "CodexAgent"),
        ("grok_agent", "GrokAgent"),
        ("claude_api", "ClaudeApiAgent"),
    ):
        monkeypatch.setattr(f"walkietalk.{module}.{name}", forbidden)


def fake_cli(monkeypatch, *, auth=AUTH, answer="Rain falls from clouds.", mutate=None, exit_code=0):
    calls = []
    monkeypatch.setattr("walkietalk.claude.shutil.which", lambda *args, **kwargs: "/fake/claude")

    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert Path(kwargs["cwd"]).is_dir()
        if "--help" in command:
            return 0, " ".join(REQUIRED_FLAGS).encode(), b""
        if "auth" in command:
            return 0, json.dumps(auth).encode(), b""
        session_id = command[command.index("--session-id") + 1]
        model = command[command.index("--model") + 1]
        kwargs["system"] = Path(command[command.index("--system-prompt-file") + 1]).read_text()
        events = [
            {
                "type": "system",
                "subtype": "init",
                "session_id": session_id,
                "model": model,
                "tools": [],
                "mcp_servers": [],
                "plugins": [],
            },
            {
                "type": "assistant",
                "session_id": session_id,
                "message": {"model": model, "content": [{"type": "text", "text": answer}]},
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": session_id,
                "result": answer,
                "permission_denials": [],
            },
        ]
        if mutate:
            mutate(events)
        return (
            exit_code,
            b"\n".join(json.dumps(event).encode() for event in events),
            SECRET.encode(),
        )

    monkeypatch.setattr("walkietalk.claude.run_cli", run)
    return calls


def test_official_login_isolated_flags_stdin_history_and_no_secrets(monkeypatch, config, capsys):
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDECODE",
        "XAI_API_KEY",
    ):
        monkeypatch.setenv(name, SECRET)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/official/cli/config")
    calls = fake_cli(monkeypatch)
    config = replace(config, agent_history_turns=1)
    agent = open_agent(config)
    assert isinstance(agent, ClaudeCodeAgent)
    session = AgentSession(config, agent)
    traffic = "--resume desktop; $(touch /tmp/never-execute)"
    for text in (traffic, "Why?", "Again?"):
        assert session.reply(text) == "Rain falls from clouds."
    prompts = [(cmd, kw) for cmd, kw in calls if "-p" in cmd]
    cmd, kw = prompts[0]
    assert traffic not in cmd and json.loads(kw["prompt"])["traffic"] == traffic
    assert "spoken-style" in kw["system"] and "600 characters" in kw["system"]
    assert kw["env"]["CLAUDE_CONFIG_DIR"] == "/official/cli/config"
    assert SECRET not in json.dumps(kw["env"])
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert cmd[cmd.index("--effort") + 1] == "low"
    assert cmd[cmd.index("--permission-prompts") + 1] == "none"
    assert all(
        flag in cmd
        for flag in (
            "--safe-mode",
            "--restricted",
            "--strict-mcp-config",
            "--no-session-persistence",
        )
    )
    assert all(
        flag not in cmd
        for flag in (
            "--bare",
            "--resume",
            "--continue",
            "--fallback-model",
            "--dangerously-skip-permissions",
        )
    )
    assert json.loads(prompts[-1][1]["prompt"])["history"] == [
        {"user": "Why?", "assistant": "Rain falls from clouds."}
    ]
    assert len({json.loads(kw["prompt"])["radio_session"] for _, kw in prompts}) == 1
    assert all(not Path(kw["cwd"]).exists() for _, kw in calls)
    assert len({kw["deadline"] for _, kw in calls[:3]}) == 1
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    assert cli.main(["-c", "unused", "agent-check", "Hello"]) == 0
    out = capsys.readouterr()
    assert "Reply: Rain falls from clouds." in out.out and SECRET not in out.out + out.err


@pytest.mark.parametrize(
    "auth",
    [
        None,
        {},
        {**AUTH, "loggedIn": False},
        {**AUTH, "authMethod": "api_key"},
        {**AUTH, "apiProvider": "bedrock"},
    ],
)
def test_login_failures_never_request_answer_or_fall_back(monkeypatch, config, auth):
    calls = fake_cli(monkeypatch, auth=auth)
    with pytest.raises(WalkietalkError, match="saved Claude account login"):
        AgentSession(config, open_agent(config)).reply("hello")
    assert len(calls) == 2 and all("-p" not in cmd for cmd, _ in calls)


def test_old_cli_is_rejected_before_auth_or_generation(monkeypatch, config):
    fake_cli(monkeypatch)
    monkeypatch.setattr("walkietalk.claude.run_cli", lambda *a, **k: (0, b"old help", b""))
    with pytest.raises(WalkietalkError, match="required isolation options"):
        AgentSession(config, open_agent(config)).reply("hello")


def test_missing_cli(monkeypatch, config):
    monkeypatch.setattr("walkietalk.claude.shutil.which", lambda *a, **k: None)
    with pytest.raises(WalkietalkError, match="CLI not found"):
        AgentSession(config, open_agent(config)).reply("hello")


@pytest.mark.parametrize("text", ["", "x" * 601, "\x1b[31mred"])
def test_invalid_final_text_not_retained(monkeypatch, config, text):
    fake_cli(monkeypatch, answer=text)
    session = AgentSession(config, open_agent(config))
    with pytest.raises(WalkietalkError):
        session.reply("hello")
    assert session.history == ()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e[0].update(tools=["Bash"]),
        lambda e: e[0].update(mcp_servers=[{"name": "desktop"}]),
        lambda e: e[0].update(plugins=["desktop"]),
        lambda e: e[0].update(model="claude-other-model"),
        lambda e: e[0].update(session_id="desktop"),
        lambda e: e[1].update(
            message={"model": "claude-sonnet-5", "content": [{"type": "tool_use"}]}
        ),
        lambda e: e[1].update(error=SECRET),
        lambda e: e[2].update(subtype="error_max_turns"),
        lambda e: e[2].update(is_error=True),
        lambda e: e[2].update(stop_reason="max_tokens"),
        lambda e: e[2].update(permission_denials=[SECRET]),
        lambda e: e[2].update(session_id="desktop"),
        lambda e: e.pop(),
        lambda e: e.append(e[-1]),
        lambda e: e.insert(1, {"type": "system", "subtype": "hook_response"}),
    ],
)
def test_unexpected_outputs_never_retained_or_disclosed(monkeypatch, config, mutate):
    fake_cli(monkeypatch, mutate=mutate)
    session = AgentSession(config, open_agent(config))
    with pytest.raises(WalkietalkError) as error:
        session.reply("hello")
    assert session.history == () and SECRET not in str(error.value)


def test_failed_cli_diagnostics_not_reply(monkeypatch, config):
    fake_cli(monkeypatch, exit_code=1)
    session = AgentSession(config, open_agent(config))
    with pytest.raises(WalkietalkError, match="Claude Code failed") as error:
        session.reply("hello")
    assert session.history == () and SECRET not in str(error.value)


def test_default_effort_does_not_override_model(monkeypatch, config):
    calls = fake_cli(monkeypatch)
    config = replace(config, claude_reasoning_effort="default")
    AgentSession(config, open_agent(config)).reply("hello")
    assert "--effort" not in calls[-1][0]


def test_rate_limit_metadata_does_not_become_reply(monkeypatch, config):
    def mutate(events):
        events.insert(1, {"type": "rate_limit_event", "rate_limit_info": SECRET})
        events.append({"type": "rate_limit_event", "rate_limit_info": SECRET})

    fake_cli(monkeypatch, mutate=mutate)
    assert AgentSession(config, open_agent(config)).reply("hello") == "Rain falls from clouds."


def test_real_subprocess_deadline_kills_fake_cli(tmp_path, config):
    executable = tmp_path / "fake-claude"
    executable.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    executable.chmod(0o700)
    config = replace(config, claude_executable=str(executable), agent_timeout_seconds=0.1)
    session = AgentSession(config, open_agent(config))
    start = time.monotonic()
    with pytest.raises(WalkietalkError, match="timed out"):
        session.reply("hello")
    assert time.monotonic() - start < 2 and session.history == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("claude_executable", "claude -p"),
        ("claude_executable", "./claude"),
        ("claude_model", "sonnet"),
        ("claude_api_model", ""),
        ("claude_api_key_env", "sk-ant-api123-secret"),
        ("claude_api_key_env", "A\nB"),
        ("claude_reasoning_effort", "ultra"),
        ("claude_api_reasoning_effort", "none"),
    ],
)
def test_claude_configuration_is_exact_and_validated(tmp_path, field, value):
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["agent"][field] = value
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(WalkietalkError, match=field):
        load_config(path)


def test_claude_config_selects_official_cli(tmp_path):
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["agent"]["backend"] = "claude"
    path = tmp_path / "claude.yaml"
    path.write_text(yaml.safe_dump(data))
    config = load_config(path)
    agent = open_agent(config)
    assert isinstance(agent, ClaudeCodeAgent)
    assert agent.label().startswith("claude (")
    assert config.claude_executable == "claude"
    assert config.claude_reasoning_effort == "low"
