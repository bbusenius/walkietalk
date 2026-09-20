import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from walkietalk import cli
from walkietalk.agent import AgentSession, open_agent
from walkietalk.agent_process import run_cli
from walkietalk.codex import CodexAgent, parse_completion
from walkietalk.config import Config, WalkietalkError, load_config

THREAD = "11111111-1111-4111-8111-111111111111"
SECRET = "private-diagnostic-not-an-answer"


@pytest.fixture
def fake_cli(tmp_path):
    executable = tmp_path / "fake codex"
    settings = tmp_path / "settings.json"
    record = tmp_path / "calls.jsonl"
    settings.write_text("{}")
    executable.write_text(f"""#!{sys.executable}
import json, os, sys, time, uuid
from pathlib import Path
settings = json.loads(Path({str(settings)!r}).read_text())
args = sys.argv[1:]
if args == ['login', 'status']:
    print(settings.get('login', 'Logged in using ChatGPT'), file=sys.stderr)
    sys.exit(settings.get('login_code', 0))
prompt = sys.stdin.read()
with open({str(record)!r}, 'a') as stream:
    stream.write(json.dumps({{'args': args, 'prompt': prompt, 'env': dict(os.environ),
        'cwd': os.getcwd()}}) + '\\n')
if settings.get('hang'):
    time.sleep(30)
final = Path(args[args.index('--output-last-message') + 1])
if not settings.get('no_final'):
    final.write_bytes(settings.get('answer', 'Rain falls from clouds.').encode())
thread = args[args.index('resume') + 1] if 'resume' in args else str(uuid.uuid4())
if settings.get('wrong_thread'):
    thread = str(uuid.uuid4())
events = settings.get('events', [
    {{'type': 'thread.started', 'thread_id': thread}},
    {{'type': 'item.completed', 'item': {{'text': {SECRET!r}}}}},
    {{'type': 'turn.completed'}}])
for event in events:
    print(json.dumps(event))
print({SECRET!r}, file=sys.stderr)
sys.exit(settings.get('code', 0))
""")
    executable.chmod(0o700)
    config = replace(Config(), agent_backend="codex", codex_executable=str(executable))

    def configure(**values):
        settings.write_text(json.dumps(values))

    def calls():
        return (
            [json.loads(line) for line in record.read_text().splitlines()]
            if record.exists()
            else []
        )

    return config, configure, calls


def test_actual_process_final_only_stdin_safe_environment_and_explicit_resume(
    fake_cli, monkeypatch
):
    config, _, calls = fake_cli
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "WALKIETALK_HERMES_TOKEN"):
        monkeypatch.setenv(key, SECRET)
    agent = open_agent(config)
    assert isinstance(agent, CodexAgent)
    session = AgentSession(config, agent)
    traffic = 'What is rain? $(touch injected) `echo hello` " ; exit'
    assert session.reply(traffic) == "Rain falls from clouds."
    first_thread = agent._thread_id
    assert session.reply("Why does that happen?") == "Rain falls from clouds."
    first, second = calls()
    assert traffic not in first["args"]
    assert json.loads(first["prompt"].splitlines()[-1])["traffic"] == traffic
    assert "short, plain, spoken-style" in first["prompt"]
    assert "resume" not in first["args"]
    assert second["args"][second["args"].index("resume") + 1] == first_thread
    assert "--last" not in second["args"]
    assert first["args"][first["args"].index("--sandbox") + 1] == "read-only"
    for setting in (
        'forced_login_method="chatgpt"',
        'approval_policy="never"',
        "features.shell_tool=false",
        "features.apps=false",
        "features.hooks=false",
        "agents.enabled=false",
        'web_search="disabled"',
    ):
        assert setting in first["args"]
    assert "--ignore-user-config" in first["args"] and "--strict-config" in first["args"]
    assert SECRET not in json.dumps(first["env"])
    assert not Path(first["cwd"]).exists()
    assert len(session.history) == 2


def test_context_rotates_at_budget_and_new_radio_session_never_resumes(fake_cli):
    config, _, calls = fake_cli
    config = replace(config, agent_history_turns=2)
    agent = open_agent(config)
    session = AgentSession(config, agent)
    for text in ("first", "second", "third", "fourth"):
        session.reply(text)
    records = calls()
    assert "resume" in records[1]["args"]
    for index in (2, 3):
        assert "resume" not in records[index]["args"]
        payload = json.loads(records[index]["prompt"].splitlines()[-1])
        assert len(payload["history"]) == 2
    assert json.loads(records[3]["prompt"].splitlines()[-1])["history"][0]["user"] == "second"
    AgentSession(config, agent).reply("new conversation")
    assert "resume" not in calls()[-1]["args"]
    assert json.loads(calls()[-1]["prompt"].splitlines()[-1])["history"] == []


@pytest.mark.parametrize("login,code", [("Logged in using an API key", 0), (SECRET, 1), ("", 0)])
def test_login_failure_or_api_login_never_launches_exec(fake_cli, login, code):
    config, configure, calls = fake_cli
    configure(login=login, login_code=code)
    with pytest.raises(WalkietalkError, match="saved ChatGPT login") as error:
        AgentSession(config, open_agent(config)).reply("hello")
    assert SECRET not in str(error.value)
    assert calls() == []


@pytest.mark.parametrize(
    "options",
    [
        {"code": 1},
        {"no_final": True},
        {"answer": ""},
        {"answer": "x" * 601},
        {"answer": "\x1b[31msecret"},
        {"events": []},
        {"events": [{"type": "error", "message": SECRET}]},
        {"events": [{"type": "turn.failed", "error": SECRET}]},
        {"events": [{"type": "approval.request", "message": SECRET}]},
        {"answer": "x" * 65537},
    ],
)
def test_bad_final_failed_turn_and_diagnostics_are_not_answers(fake_cli, options):
    config, configure, _ = fake_cli
    configure(**options)
    session = AgentSession(config, open_agent(config))
    with pytest.raises(WalkietalkError) as error:
        session.reply("hello")
    assert SECRET not in str(error.value)
    assert session.history == ()
    assert session.backend._thread_id is None


def test_wrong_resumed_thread_is_discarded_and_failure_resets_session(fake_cli):
    config, configure, calls = fake_cli
    agent = open_agent(config)
    session = AgentSession(config, agent)
    session.reply("first")
    configure(wrong_thread=True)
    with pytest.raises(WalkietalkError, match="different session"):
        session.reply("followup")
    configure()
    session.reply("retry")
    assert "resume" not in calls()[-1]["args"]
    assert len(json.loads(calls()[-1]["prompt"].splitlines()[-1])["history"]) == 1


@pytest.mark.parametrize(
    "events",
    [
        b"not json",
        b"[]",
        b'{"type":"thread.started"}',
        b'{"type":"thread.started","thread_id":"bad"}',
        json.dumps({"type": "thread.started", "thread_id": THREAD}).encode(),
        b'{"type":"turn.completed"}',
    ],
)
def test_malformed_or_incomplete_events_are_rejected(events):
    with pytest.raises(WalkietalkError):
        parse_completion(events)


def test_missing_executable_has_clear_local_error(tmp_path):
    config = replace(Config(), agent_backend="codex", codex_executable=str(tmp_path / "missing"))
    with pytest.raises(WalkietalkError, match="CLI not found"):
        AgentSession(config, open_agent(config)).reply("hello")


@pytest.mark.parametrize(
    "field,value",
    [
        ("codex_executable", ""),
        ("codex_executable", "codex -p"),
        ("codex_executable", "./codex"),
        ("codex_executable", 3),
        ("codex_model", "--bad"),
        ("codex_model", "a\nb"),
        ("codex_model", None),
    ],
)
def test_bad_config_rejected(tmp_path, field, value):
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["agent"][field] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(WalkietalkError, match="agent.codex"):
        load_config(path)


def test_cli_errors_never_touch_radio_or_print_diagnostics(fake_cli, monkeypatch, capsys):
    config, configure, _ = fake_cli

    def forbidden(*args, **kwargs):
        pytest.fail("agent-check accessed hardware")

    for name in ("SerialPTT", "DryPTT", "Playback", "transmit", "open_stt", "preflight"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    for options in ({"code": 1}, {"answer": "x" * 601}, {"login_code": 1}):
        configure(**options)
        assert cli.main(["-c", "unused.yaml", "agent-check", "What is rain?"]) == 1
        output = capsys.readouterr()
        assert "Reply:" not in output.out
        assert SECRET not in output.out + output.err
        assert output.err


def run_python(code, tmp_path, *, prompt=b"", timeout=0.2, final_path=None):
    return run_cli(
        [sys.executable, "-c", code],
        prompt=prompt,
        cwd=str(tmp_path),
        env={"PATH": os.environ["PATH"]},
        deadline=time.monotonic() + timeout,
        final_path=final_path,
    )


@pytest.mark.parametrize(
    "code,prompt",
    [
        ("import time; time.sleep(30)", b"x" * 1_000_000),
        ("import os,time; os.close(1); os.close(2); time.sleep(30)", b""),
        (
            'import subprocess,sys; subprocess.Popen([sys.executable,"-c",'
            '"import time; time.sleep(30)"])',
            b"",
        ),
    ],
)
def test_deadline_covers_blocked_stdin_closed_pipes_and_inherited_pipes(tmp_path, code, prompt):
    start = time.monotonic()
    with pytest.raises(WalkietalkError, match="timed out"):
        run_python(code, tmp_path, prompt=prompt)
    assert time.monotonic() - start < 2


@pytest.mark.parametrize("pipe", ["stdout", "stderr"])
def test_transport_output_is_bounded(tmp_path, pipe):
    with pytest.raises(WalkietalkError, match="transport limit"):
        run_python(f'import sys; sys.{pipe}.write("x" * 1100000)', tmp_path, timeout=2)


def test_oversized_final_file_is_detected_while_process_hangs(tmp_path):
    final_path = tmp_path / "final"
    with pytest.raises(WalkietalkError, match="transport limit"):
        run_python(
            "from pathlib import Path; import time; "
            'Path("final").write_text("x" * 65537); time.sleep(30)',
            tmp_path,
            timeout=2,
            final_path=final_path,
        )


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_stop_signal_kills_cli_process_group(tmp_path, sig):
    pidfile = tmp_path / "child.pid"
    parent = tmp_path / "parent.py"
    child_code = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    parent.write_text(f"""
import sys, time
from walkietalk.agent_process import run_cli
from walkietalk.session import handle_stop_signals
try:
    with handle_stop_signals():
        run_cli([sys.executable, '-c', {child_code!r}], prompt=b'', cwd={str(tmp_path)!r},
                env={{}}, deadline=time.monotonic()+20)
except KeyboardInterrupt:
    sys.exit(130)
""")
    process = subprocess.Popen(
        [sys.executable, str(parent)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        deadline = time.monotonic() + 3
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pidfile.exists()
        pid = int(pidfile.read_text())
        process.send_signal(sig)
        out, err = process.communicate(timeout=3)
        assert process.returncode == 130, (out, err)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def test_adapter_timeout_discards_turn_and_does_not_record_history(fake_cli):
    config, configure, _ = fake_cli
    configure(hang=True)
    config = replace(config, agent_timeout_seconds=0.2)
    session = AgentSession(config, open_agent(config))
    with pytest.raises(WalkietalkError, match="timed out"):
        session.reply("hello")
    assert session.history == ()
    assert session.backend._thread_id is None


def test_timeout_kills_descendant_that_ignores_term(tmp_path):
    pidfile = tmp_path / "descendant.pid"
    child = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    code = f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {child!r}])"
    with pytest.raises(WalkietalkError, match="timed out"):
        run_python(code, tmp_path, timeout=0.4)
    pid = int(pidfile.read_text())
    status = Path(f"/proc/{pid}/stat")
    deadline = time.monotonic() + 1
    while status.exists() and status.read_text().split()[2] != "Z":
        assert time.monotonic() < deadline, "descendant survived group cleanup"
        time.sleep(0.01)


@pytest.mark.parametrize("effort", ["default", "low", "medium", "high", "xhigh", "max"])
def test_explicit_reasoning_on_new_and_resumed_cli_turns(fake_cli, effort):
    config, _, calls = fake_cli
    config = replace(config, codex_reasoning_effort=effort)
    agent = open_agent(config)
    session = AgentSession(config, agent)
    session.reply("first")
    session.reply("followup")
    for call in calls():
        overrides = [arg for arg in call["args"] if arg.startswith("model_reasoning_effort=")]
        assert overrides == ([] if effort == "default" else [f'model_reasoning_effort="{effort}"'])
    assert f"reasoning={effort}" in agent.label()
