import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from walkietalk import cli
from walkietalk.agent import AgentSession, open_agent
from walkietalk.config import Config, WalkietalkError, load_config
from walkietalk.grok_agent import GrokAgent, inspect_profile, parse_completion

THREAD = "11111111-1111-4111-8111-111111111111"
SECRET = "private-diagnostic-not-an-answer"
PROFILE = {
    "loginPolicy": {"apiKeyAuthDisabled": True},
    "hooks": [],
    "mcpServers": [],
    "plugins": [],
    "lspServers": [],
    "configSources": {"layers": []},
}


def events(thread=THREAD, answer="Rain falls from clouds."):
    return [
        {
            "type": "system",
            "subtype": "init",
            "session_id": thread,
            "apiKeySource": "oauth",
            "permissionMode": "dontAsk",
            "mcp_servers": [],
        },
        {
            "type": "assistant",
            "session_id": thread,
            "message": {
                "content": [
                    {"type": "thinking", "thinking": SECRET},
                    {"type": "text", "text": "ignored intermediate text"},
                ]
            },
        },
        {
            "type": "result",
            "session_id": thread,
            "subtype": "success",
            "is_error": False,
            "stop_reason": "end_turn",
            "result": answer,
        },
    ]


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    home = tmp_path / "grok-home"
    home.mkdir()
    (home / "auth.json").write_text("CLI-owned fake login; adapter must not parse this")
    monkeypatch.setenv("GROK_HOME", str(home))
    executable = tmp_path / "fake grok"
    settings = tmp_path / "settings.json"
    record = tmp_path / "calls.jsonl"
    settings.write_text("{}")
    executable.write_text(f"""#!{sys.executable}
import json,os,sys,time,uuid
from pathlib import Path
settings=json.loads(Path({str(settings)!r}).read_text())
args=sys.argv[1:]
if args == ['inspect', '--json']:
    if settings.get('inspect_hang'): time.sleep(30)
    print(json.dumps(settings.get('profile', {PROFILE!r})))
    sys.exit(settings.get('inspect_code', 0))
prompt=Path(args[args.index('--prompt-file')+1]).read_text()
with open({str(record)!r}, 'a') as stream:
    stream.write(json.dumps({{'args':args,'prompt':prompt,'env':dict(os.environ),
        'cwd':os.getcwd()}})+'\\n')
if settings.get('hang'): time.sleep(30)
thread=args[args.index('--resume' if '--resume' in args else '--session-id')+1]
if settings.get('wrong_thread'): thread=str(uuid.uuid4())
frames={events()!r}
for frame in frames:
    frame['session_id']=thread
frames[0]['apiKeySource']=settings.get('auth', 'oauth')
frames[0]['permissionMode']=settings.get('permission', 'dontAsk')
frames[-1]['result']=settings.get('answer','Rain falls from clouds.')
for frame in settings.get('events',frames): print(json.dumps(frame))
print({SECRET!r}, file=sys.stderr)
sys.exit(settings.get('code',0))
""")
    executable.chmod(0o700)
    config = replace(Config(), agent_backend="grok", grok_executable=str(executable))

    def configure(**values):
        settings.write_text(json.dumps(values))

    def calls():
        return (
            [json.loads(line) for line in record.read_text().splitlines()]
            if record.exists()
            else []
        )

    return config, configure, calls, home


def test_final_only_stdin_environment_and_explicit_followup(fake_cli, monkeypatch):
    config, _, calls, home = fake_cli
    for key in (
        "XAI_API_KEY",
        "OPENAI_API_KEY",
        "GROK_CONFIG",
        "GROK_AUTH_PROVIDER_COMMAND",
        "GROK_MODELS_BASE_URL",
        "WALKIETALK_HERMES_TOKEN",
    ):
        monkeypatch.setenv(key, SECRET)
    agent = open_agent(config)
    assert isinstance(agent, GrokAgent)
    session = AgentSession(config, agent)
    text = 'What is rain? $(touch injected) `echo hi` " ; exit @private-file'
    assert session.reply(text) == "Rain falls from clouds."
    first_thread = agent._thread_id
    assert session.reply("Why does that happen?") == "Rain falls from clouds."
    first, second = calls()
    assert json.loads(first["prompt"].splitlines()[-1])["traffic"] == text
    assert text not in first["args"]
    assert "short, plain, spoken-style" in first["prompt"]
    assert "--verbatim" in first["args"]
    assert first["args"][first["args"].index("--session-id") + 1] == first_thread
    assert second["args"][second["args"].index("--resume") + 1] == first_thread
    assert "--continue" not in second["args"]
    assert first["args"][first["args"].index("--permission-mode") + 1] == "dontAsk"
    assert first["args"][first["args"].index("--deny") + 1] == "*"
    assert first["args"][first["args"].index("--sandbox") + 1] == "read-only"
    assert "--disable-web-search" in first["args"]
    assert first["args"][first["args"].index("--max-turns") + 1] == "1"
    assert "Do not search the web." in first["prompt"]
    assert first["env"]["GROK_DISABLE_API_KEY_AUTH"] == "1"
    assert first["env"]["GROK_CLAUDE_MCPS_ENABLED"] == "0"
    assert first["env"]["GROK_HOME"] == str(home)
    assert first["env"]["HOME"] == str(Path(first["cwd"]) / "home")
    assert "XDG_CONFIG_HOME" not in first["env"]
    assert (home / "auth.json").read_text() == "CLI-owned fake login; adapter must not parse this"
    assert SECRET not in json.dumps(first["env"])
    assert not Path(first["cwd"]).exists()
    assert len(session.history) == 2


def test_web_search_allows_lookup_and_keeps_shell_closed(fake_cli):
    config, _, calls, _ = fake_cli
    config = replace(config, agent_web_search=True)
    assert AgentSession(config, open_agent(config)).reply("weather") == "Rain falls from clouds."
    record = calls()[-1]
    args = record["args"]
    assert args[args.index("--sandbox") + 1] == "workspace"
    assert args[args.index("--tools") + 1] == "web_search"
    assert "--disable-web-search" not in args
    assert "*" not in args
    assert args[args.index("--max-turns") + 1] == "4"
    for rule in ("Bash", "Read", "Edit", "Grep", "MCPTool", "WebFetch"):
        assert rule in args
    assert "You may search the public web." in record["prompt"]
    assert "Do not use tools" not in record["prompt"]
    assert record["env"]["GROK_WEB_FETCH"] == "0"


def test_lookup_blocks_are_accepted_only_when_search_is_enabled():
    frame = events()
    frame[1]["message"]["content"] = [
        {"type": "server_tool_use", "name": "web_search", "input": {"query": "rain"}},
        {"type": "web_search_tool_result", "content": [{"url": "https://secret.example"}]},
        {"type": "tool_use", "name": "web_search", "input": {"query": "rain"}},
        {"type": "text", "text": "ignored"},
    ]
    frame.insert(
        2,
        {
            "type": "user",
            "session_id": THREAD,
            "message": {"content": [{"type": "tool_result", "content": "https://secret.example"}]},
        },
    )
    raw = b"\n".join(json.dumps(item).encode() for item in frame)
    with pytest.raises(WalkietalkError):
        parse_completion(raw, THREAD)
    assert parse_completion(raw, THREAD, web_search=True) == "Rain falls from clouds."
    frame[1]["message"]["content"].append({"type": "tool_use", "name": "bash"})
    raw = b"\n".join(json.dumps(item).encode() for item in frame)
    with pytest.raises(WalkietalkError):
        parse_completion(raw, THREAD, web_search=True)


def test_bounded_history_rotation_and_new_radio_session(fake_cli):
    config, _, calls, _ = fake_cli
    config = replace(config, agent_history_turns=2)
    agent = open_agent(config)
    session = AgentSession(config, agent)
    for text in ("first", "second", "third", "fourth"):
        session.reply(text)
    records = calls()
    assert "--resume" in records[1]["args"]
    for index in (2, 3):
        assert "--session-id" in records[index]["args"]
        payload = json.loads(records[index]["prompt"].splitlines()[-1])
        assert len(payload["history"]) == 2
    assert json.loads(records[3]["prompt"].splitlines()[-1])["history"][0]["user"] == "second"
    AgentSession(config, agent).reply("new conversation")
    assert "--resume" not in calls()[-1]["args"]
    assert json.loads(calls()[-1]["prompt"].splitlines()[-1])["history"] == []


@pytest.mark.parametrize(
    "options",
    [
        {"code": 1},
        {"auth": "user"},
        {"permission": "bypassPermissions"},
        {"answer": ""},
        {"answer": "x" * 601},
        {"answer": "\x1b[31msecret"},
        {"events": []},
        {"events": [{"type": "error", "message": SECRET}]},
        {"wrong_thread": True},
    ],
)
def test_invalid_replies_and_failed_logins_do_not_enter_context(fake_cli, options):
    config, configure, _calls, _home = fake_cli
    configure(**options)
    session = AgentSession(config, open_agent(config))
    with pytest.raises(WalkietalkError) as error:
        session.reply("hello")
    assert SECRET not in str(error.value)
    assert session.history == ()
    assert session.backend._thread_id is None


def test_failed_turn_never_resumed(fake_cli):
    config, configure, calls, _ = fake_cli
    agent = open_agent(config)
    session = AgentSession(config, agent)
    session.reply("first")
    configure(code=1)
    with pytest.raises(WalkietalkError):
        session.reply("failed followup")
    configure()
    session.reply("retry")
    assert "--resume" not in calls()[-1]["args"]
    assert len(json.loads(calls()[-1]["prompt"].splitlines()[-1])["history"]) == 1


def test_missing_login_never_launches_inference_or_uses_api_key(fake_cli, monkeypatch):
    config, _, calls, home = fake_cli
    (home / "auth.json").unlink()
    monkeypatch.setenv("XAI_API_KEY", SECRET)
    with pytest.raises(WalkietalkError, match="saved login is missing"):
        AgentSession(config, open_agent(config)).reply("hello")
    assert calls() == []


def test_missing_executable_clear_local_error(tmp_path):
    config = replace(Config(), agent_backend="grok", grok_executable=str(tmp_path / "absent"))
    with pytest.raises(WalkietalkError, match="Grok Build CLI not found"):
        AgentSession(config, open_agent(config)).reply("hello")


@pytest.mark.parametrize("scenario", ["inspect_hang", "hang"])
def test_shared_deadline_covers_inspection_and_generation(fake_cli, scenario):
    config, configure, _, _ = fake_cli
    configure(**{scenario: True})
    config = replace(config, agent_timeout_seconds=0.2)
    start = time.monotonic()
    session = AgentSession(config, open_agent(config))
    with pytest.raises(WalkietalkError, match="Grok Build timed out"):
        session.reply("hello")
    assert time.monotonic() - start < 2
    assert session.history == ()


@pytest.mark.parametrize("key", ["hooks", "mcpServers", "plugins", "lspServers"])
def test_active_integrations_block_inference(fake_cli, key):
    config, configure, calls, _ = fake_cli
    profile = {**PROFILE, key: [{"name": SECRET, "disabled": False}]}
    configure(profile=profile)
    with pytest.raises(WalkietalkError, match="requires inactive") as error:
        AgentSession(config, open_agent(config)).reply("hello")
    assert SECRET not in str(error.value)
    assert calls() == []
    profile[key][0]["disabled"] = True
    configure(profile=profile)
    assert AgentSession(config, open_agent(config)).reply("hello") == "Rain falls from clouds."


@pytest.mark.parametrize(
    "profile",
    [
        {},
        {**PROFILE, "loginPolicy": {"apiKeyAuthDisabled": False}},
        {**PROFILE, "hooks": None},
        {**PROFILE, "mcpServers": [None]},
    ],
)
def test_unverifiable_policy_blocks_question(fake_cli, profile):
    config, configure, calls, _ = fake_cli
    configure(profile=profile)
    with pytest.raises(WalkietalkError, match="Cannot verify"):
        AgentSession(config, open_agent(config)).reply("hello")
    assert calls() == []


def test_custom_provider_credentials_rejected_before_question(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[model.custom]\napi_key="' + SECRET + '"\n')
    profile = {**PROFILE, "configSources": {"layers": [{"path": str(path)}]}}
    with pytest.raises(WalkietalkError, match="standard first-party") as error:
        inspect_profile(json.dumps(profile).encode())
    assert SECRET not in str(error.value)


@pytest.mark.parametrize(
    "mutation",
    [
        "no_init",
        "no_result",
        "extra_after_result",
        "tool_call",
        "error_result",
        "max_tokens",
        "cancelled",
        "wrong_id",
        "mcp_connected",
        "non_json",
    ],
)
def test_only_complete_final_result_accepted(mutation):
    frames = events()
    if mutation == "no_init":
        frames = frames[1:]
    elif mutation == "no_result":
        frames = frames[:-1]
    elif mutation == "extra_after_result":
        frames.append(frames[1])
    elif mutation == "tool_call":
        frames[1]["message"]["content"] = [{"type": "tool_use", "name": "bash"}]
    elif mutation == "error_result":
        frames[-1]["is_error"] = True
    elif mutation in ("max_tokens", "cancelled"):
        frames[-1]["stop_reason"] = mutation
    elif mutation == "wrong_id":
        frames[-1]["session_id"] = "wrong"
    elif mutation == "mcp_connected":
        frames[0]["mcp_servers"] = [{"status": "connected"}]
    raw = b"\n".join(json.dumps(frame).encode() for frame in frames)
    if mutation == "non_json":
        raw += b"\n" + SECRET.encode()
    with pytest.raises(WalkietalkError) as error:
        parse_completion(raw, THREAD)
    assert SECRET not in str(error.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("grok_executable", ""),
        ("grok_executable", "grok -p"),
        ("grok_executable", "./grok"),
        ("grok_executable", False),
        ("grok_model", ""),
        ("grok_model", "other-provider"),
        ("grok_model", "grok-foo\n"),
        ("grok_model", None),
    ],
)
def test_bad_config_rejected(tmp_path, field, value):
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["agent"][field] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(WalkietalkError, match="agent.grok"):
        load_config(path)


def test_cli_failures_never_touch_radio_or_print_diagnostics(fake_cli, monkeypatch, capsys):
    config, configure, _, _ = fake_cli

    def forbidden(*args, **kwargs):
        pytest.fail("agent-check accessed hardware")

    for name in ("SerialPTT", "DryPTT", "Playback", "transmit", "open_stt", "preflight"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    for options in ({"code": 1}, {"answer": "x" * 601}, {"auth": "user"}):
        configure(**options)
        assert cli.main(["-c", "unused.yaml", "agent-check", "What is rain?"]) == 1
        output = capsys.readouterr()
        assert "Reply:" not in output.out
        assert SECRET not in output.out + output.err
        assert output.err


@pytest.mark.parametrize("effort", ["default", "none", "low", "medium", "high", "xhigh", "max"])
def test_explicit_reasoning_on_new_and_resumed_cli_turns(fake_cli, effort):
    config, _, calls, _ = fake_cli
    config = replace(config, grok_reasoning_effort=effort)
    agent = open_agent(config)
    session = AgentSession(config, agent)
    session.reply("first")
    session.reply("followup")
    for call in calls():
        args = call["args"]
        if effort == "default":
            assert "--reasoning-effort" not in args
        else:
            assert args[args.index("--reasoning-effort") + 1] == effort
    assert f"reasoning={effort}" in agent.label()
