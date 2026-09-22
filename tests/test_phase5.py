from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from walkietalk import cli
from walkietalk.agent import (
    MAX_TRAFFIC_CHARS,
    STUB_REPLY,
    AgentSession,
    StubAgent,
    Turn,
    open_agent,
)
from walkietalk.capture import Utterance
from walkietalk.config import Config, WalkietalkError, load_config
from walkietalk.wake import ListeningSession


@pytest.fixture(autouse=True)
def no_transmission(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("agent path touched serial, playback, or transmit")

    monkeypatch.setattr(cli, "SerialPTT", forbidden)
    monkeypatch.setattr(cli, "DryPTT", forbidden)
    monkeypatch.setattr(cli, "Playback", forbidden)
    monkeypatch.setattr(cli, "transmit", forbidden)


def test_stub_check_is_offline_and_independent_of_stt(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("agent-check touched STT, audio, or network")

    for name in ("preflight", "open_stt", "capture_from_device", "capture_from_wav"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr("socket.socket", forbidden)
    assert cli.main(["agent-check", "What is rain?"]) == 0
    output = capsys.readouterr().out
    assert f"Reply: {STUB_REPLY}" in output
    assert "stub (offline pretend answer)" in output


@pytest.mark.parametrize("backend", ["claude_code", "typo"])
def test_unimplemented_agents_never_fall_back(backend):
    with pytest.raises(WalkietalkError, match="not implemented"):
        open_agent(replace(Config(), agent_backend=backend))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("backend", "claude_code"),
        ("max_reply_chars", 0),
        ("max_reply_chars", 2001),
        ("max_reply_chars", True),
        ("max_reply_chars", 600.5),
        ("history_turns", 0),
        ("history_turns", 33),
        ("history_turns", "8"),
        ("history_turns", False),
        ("web_search", "enabled"),
        ("web_search", 1),
        ("instructions", 1),
        ("instructions", "x" * 2001),
        ("instructions", "line\nbreak"),
        ("instructions", "Hello {name}"),
        ("instructions", "Use {max_reply_chars!s}"),
    ],
)
def test_invalid_agent_config_rejected(tmp_path, field, value):
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["agent"][field] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(WalkietalkError, match="agent"):
        load_config(path)


@pytest.mark.parametrize("change", ["missing_section", "missing_field", "secret_field"])
def test_agent_config_requires_exact_fields(tmp_path, change):
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    if change == "missing_section":
        del data["agent"]
    elif change == "missing_field":
        del data["agent"]["history_turns"]
    else:
        data["agent"]["token"] = "not-allowed-in-yaml"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(WalkietalkError, match="agent"):
        load_config(path)


def test_example_agent_config_and_offline_stub():
    config = load_config(Path("config.example.yaml"))
    assert config.agent_backend == "stub"
    assert config.agent_max_reply_chars == 600
    assert config.agent_history_turns == 8
    assert config.agent_web_search is False
    assert config.agent_instructions == ""
    assert AgentSession(config, open_agent(config)).reply("Hello") == STUB_REPLY


def test_history_is_bounded_in_complete_pairs_and_sessions_are_distinct():
    backend = Mock()
    backend.reply.return_value = "A short answer."
    config = replace(Config(), agent_history_turns=2)
    session = AgentSession(config, backend)
    for text in ("first", "second", "third", "fourth"):
        session.reply(text)
    context = backend.reply.call_args.args[1]
    assert context.history == (Turn("second", "A short answer."), Turn("third", "A short answer."))
    assert "suitable for a family" in context.instructions
    assert "spoken-style" in context.instructions
    assert "Do not search the web." in context.instructions
    assert "600 characters" in context.instructions
    assert context.session_id == session.session_id
    other = AgentSession(config, backend)
    other.reply("new conversation")
    assert backend.reply.call_args.args[1].history == ()
    assert other.session_id != session.session_id


def test_known_instruction_placeholders_load(tmp_path):
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["agent"]["instructions"] = "Use {max_reply_chars}, {spoken_seconds}, and {max_words}."
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    loaded = load_config(path)
    assert loaded.agent_instructions == "Use {max_reply_chars}, {spoken_seconds}, and {max_words}."


def test_default_instructions_name_the_family_default_and_the_radio_window():
    backend = Mock()
    backend.reply.return_value = "A short answer."
    config = replace(Config(), max_tx_seconds=20, settle_seconds=0.4)
    spoken = AgentSession(config, backend, spoken_seconds=19.6)
    spoken.reply("weather")
    text = backend.reply.call_args.args[1].instructions
    assert "suitable for a family" in text
    assert "at most 600 characters" in text
    assert "at most 39 words" in text
    assert "19.6 seconds" in text
    assert "Do not search the web." in text
    assert "Return only the final answer" in text
    quiet = AgentSession(config, backend)
    quiet.reply("weather")
    quiet_text = backend.reply.call_args.args[1].instructions
    assert "suitable for a family" in quiet_text
    assert "spoken on a radio" not in quiet_text


def test_custom_instructions_replace_guidance_and_fill_placeholders():
    backend = Mock()
    backend.reply.return_value = "A short answer."
    session = AgentSession(
        replace(
            Config(),
            max_tx_seconds=20,
            settle_seconds=0.4,
            agent_instructions=(
                "Answer fully. Stay within {max_reply_chars} characters. "
                "The radio window is {spoken_seconds} seconds and {max_words} words. "
                "A brace looks like {{max_reply_chars}}."
            ),
        ),
        backend,
        spoken_seconds=19.6,
    )
    session.reply("weather")
    text = backend.reply.call_args.args[1].instructions
    assert text.startswith(
        "Answer fully. Stay within 600 characters. "
        "The radio window is 19.6 seconds and 39 words. "
        "A brace looks like {max_reply_chars}."
    )
    assert "suitable for a family" not in text
    assert "spoken on a radio" not in text
    assert "Do not search the web." in text
    assert "Return only the final answer" in text


def test_web_search_instruction_replaces_the_ban():
    backend = Mock()
    backend.reply.return_value = "A short answer."
    session = AgentSession(replace(Config(), agent_web_search=True), backend)
    session.reply("weather")
    instructions = backend.reply.call_args.args[1].instructions
    assert "public web" in instructions
    assert "Do not run commands" in instructions
    assert "Do not search the web." not in instructions


@pytest.mark.parametrize("answer", [None, {}, "", "   ", "x" * 601, "\x1b[31mred"])
def test_invalid_reply_is_discarded_without_history_or_raw_error(answer):
    backend = Mock()
    backend.reply.return_value = STUB_REPLY
    session = AgentSession(Config(), backend)
    session.reply("first")
    before = session.history
    backend.reply.return_value = answer
    with pytest.raises(WalkietalkError) as exc:
        session.reply("second")
    assert session.history == before
    assert "\x1b" not in str(exc.value)
    assert "x" * 601 not in str(exc.value)


def test_reply_cap_boundary_and_single_line_text():
    backend = Mock()
    backend.reply.return_value = "abc\ndef"
    session = AgentSession(replace(Config(), agent_max_reply_chars=7), backend)
    assert session.reply("  hello  ") == "abc def"
    assert session.history == (Turn("hello", "abc def"),)
    backend.reply.return_value = "abcdefgh"
    with pytest.raises(WalkietalkError, match="exceeds"):
        session.reply("again")


@pytest.mark.parametrize("text", ["", "   ", "x" * (MAX_TRAFFIC_CHARS + 1)])
def test_invalid_traffic_never_reaches_backend(text):
    backend = Mock()
    session = AgentSession(Config(), backend)
    with pytest.raises(WalkietalkError):
        session.reply(text)
    backend.reply.assert_not_called()


def install_talk_fakes(monkeypatch, config, transcripts, backend):
    texts = iter(transcripts)
    listener = Mock()
    listener.label.return_value = "fake STT"
    listener.transcribe.side_effect = lambda *args: next(texts)
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "open_stt", lambda config: listener)
    monkeypatch.setattr(cli, "open_agent", lambda config: backend)
    monkeypatch.setattr(cli, "preflight", lambda config, **kwargs: None)
    monkeypatch.setattr(
        cli,
        "capture_from_device",
        lambda *args, **kwargs: Utterance(b"\x00\x40" * 320, 16000, 0.5, 0.5, 1, "silence", 100),
    )


def test_wake_is_stripped_before_agent_call(monkeypatch, capsys):
    backend = Mock()
    backend.reply.return_value = STUB_REPLY
    config = replace(Config(), wake_primary="code nine", wake_aliases=("code 9",))
    install_talk_fakes(monkeypatch, config, ["Code 9. What is rain?"], backend)
    assert cli.main(["-c", "unused.yaml", "talk", "--capture", "--once"]) == 0
    assert backend.reply.call_args.args[0] == "What is rain?"
    assert backend.reply.call_args.args[1].history == ()
    assert f"Reply: {STUB_REPLY}" in capsys.readouterr().out


@pytest.mark.parametrize("transcript", ["ordinary chatter", "charlotte", ""])
def test_ignored_empty_and_wake_only_never_call_agent(monkeypatch, transcript):
    backend = Mock()
    config = replace(Config(), listening_mode="conversation")
    install_talk_fakes(monkeypatch, config, [transcript], backend)
    assert cli.main(["-c", "unused.yaml", "talk", "--capture", "--once"]) == 0
    backend.reply.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        WalkietalkError("Agent timed out"),
        WalkietalkError("Agent login expired; sign in again"),
        WalkietalkError("Agent permission required"),
        WalkietalkError("Agent CLI not found"),
        KeyboardInterrupt(),
    ],
)
def test_agent_failure_closes_window_and_never_prints_reply(monkeypatch, capsys, failure):
    config = replace(Config(), listening_mode="conversation")
    backend = Mock()
    backend.reply.side_effect = failure
    install_talk_fakes(monkeypatch, config, ["charlotte hello"], backend)
    gate = ListeningSession(config, clock=lambda: 100)
    gate.complete_turn()
    monkeypatch.setattr(cli, "ListeningSession", lambda config: gate)
    expected = 130 if isinstance(failure, KeyboardInterrupt) else 1
    assert cli.main(["-c", "unused.yaml", "talk", "--capture", "--once"]) == expected
    assert gate.awake_until is None
    output = capsys.readouterr()
    assert "Reply:" not in output.out
    assert output.err


def test_oversized_reply_through_talk_never_opens_ptt(monkeypatch, capsys):
    config = replace(Config(), agent_max_reply_chars=10)
    install_talk_fakes(monkeypatch, config, ["charlotte hello"], StubAgent())
    assert cli.main(["-c", "unused.yaml", "talk", "--capture", "--once"]) == 1
    output = capsys.readouterr()
    assert "reply discarded" in output.err
    assert "Reply:" not in output.out
    assert STUB_REPLY not in output.out + output.err


def test_followup_context_and_window_start_after_print_and_survive_expiry(monkeypatch):
    config = replace(Config(), listening_mode="conversation", conversation_timeout_seconds=10)
    backend = Mock()
    now = [100]
    gate = ListeningSession(config, clock=lambda: now[0])
    contexts = []

    def reply(text, context):
        assert gate.awake_until is None
        contexts.append((text, context))
        now[0] += 100  # Agent processing does not consume the next window.
        return "Water falls from clouds."

    backend.reply.side_effect = reply
    install_talk_fakes(
        monkeypatch,
        config,
        ["charlotte what is rain", "why does that happen", "charlotte tell me more"],
        backend,
    )
    monkeypatch.setattr(cli, "ListeningSession", lambda config: gate)
    calls = [0]

    def capture(*args, **kwargs):
        calls[0] += 1
        if calls[0] == 2:
            assert gate.awake_until == 213  # 100 + agent 100 + display 3 + window 10
            started = 212
            now[0] = 220  # Speech began inside window; transcription ends after it.
        elif calls[0] == 3:
            now[0] = 400
            kwargs["on_wait"]()
            assert gate.awake_until is None
            started = 400
        elif calls[0] == 4:
            raise KeyboardInterrupt
        else:
            started = 100
        return Utterance(b"\x00\x40" * 320, 16000, 0.5, 0.5, 1, "silence", started)

    def emit(kind, text, **kwargs):
        if kind == "reply":
            assert gate.awake_until is None
            now[0] += 3  # Window must start after display completes.

    monkeypatch.setattr(cli, "capture_from_device", capture)
    monkeypatch.setattr(cli, "emit", emit)
    assert cli.main(["-c", "unused.yaml", "talk", "--capture"]) == 130
    assert [text for text, _ in contexts] == [
        "what is rain",
        "why does that happen",
        "tell me more",
    ]
    assert len({context.session_id for _, context in contexts}) == 1
    assert [len(context.history) for _, context in contexts] == [0, 1, 2]
    assert contexts[1][1].history[0] == Turn("what is rain", "Water falls from clouds.")


@pytest.mark.parametrize("field", ["codex_reasoning_effort", "grok_reasoning_effort"])
@pytest.mark.parametrize("value", ["", None, True, 1, [], "hgh", "LOW", "low\n--always-approve"])
def test_invalid_reasoning_configuration_rejected(tmp_path, field, value):
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["agent"][field] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(WalkietalkError, match=field):
        load_config(path)


def test_reasoning_fields_are_required_and_independent(tmp_path):
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["agent"]["codex_reasoning_effort"] = "high"
    data["agent"]["grok_reasoning_effort"] = "low"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    config = load_config(path)
    assert config.codex_reasoning_effort == "high"
    assert config.grok_reasoning_effort == "low"
    assert config.agent_timeout_seconds == 60
    assert config.agent_max_reply_chars == 600
    for field in ("codex_reasoning_effort", "grok_reasoning_effort"):
        changed = {**data, "agent": dict(data["agent"])}
        del changed["agent"][field]
        path.write_text(yaml.safe_dump(changed))
        with pytest.raises(WalkietalkError, match=field):
            load_config(path)
