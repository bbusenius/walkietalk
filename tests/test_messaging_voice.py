"""Optional contact voice conversion; fake messengers, no accounts or radio hardware."""

import base64
import json
import shutil
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import yaml

from walkietalk import cli, messaging
from walkietalk.audio import Wav
from walkietalk.capture import Utterance
from walkietalk.config import Config, MessagingMode, OutputTooLarge, WalkietalkError, load_config
from walkietalk.messaging import Inbound, MessageBridge
from walkietalk.tts import write_wav
from walkietalk.wake import ListeningSession

PCM = Wav(b"\x01\x00" * 4800, 48000, 0.1)
MODE = MessagingMode(wake="nana", to="123", sender_alias="Nana")


@pytest.mark.parametrize("service", ["whatsapp", "signal"])
@pytest.mark.parametrize("option", ["send_as_voice", "transcribe_voice"])
@pytest.mark.parametrize("value", [True, False, "true", 1, None])
def test_voice_options_are_optional_strict_booleans(tmp_path, service, option, value):
    data = yaml.safe_load(Path("src/walkietalk/data/config.example.yaml").read_text())
    data["messaging"][service].update(wake="nana", to="123")
    for field in ("send_as_voice", "transcribe_voice"):
        data["messaging"][service].pop(field, None)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    assert getattr(getattr(load_config(path), service), option) is False
    data["messaging"][service][option] = value
    path.write_text(yaml.safe_dump(data))
    if isinstance(value, bool):
        assert getattr(getattr(load_config(path), service), option) is value
    else:
        with pytest.raises(WalkietalkError, match=f"messaging.{service}.{option}"):
            load_config(path)


@pytest.fixture
def fake_encoder(monkeypatch):
    paths = []

    def encode(audio, path, maximum):
        assert audio == PCM
        paths.append(path)
        path.write_bytes(b"OggS fake opus")

    monkeypatch.setattr(messaging, "encode_voice_note", encode)
    return paths


@pytest.mark.parametrize("fail", [False, True])
def test_whatsapp_sends_voice_and_cleans_temporary_files(monkeypatch, fake_encoder, fail):
    bridge = MessageBridge(Config(whatsapp=replace(MODE, send_as_voice=True)))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        path = Path(command[command.index("--file") + 1])
        assert path.read_bytes() == b"OggS fake opus"
        assert command[command.index("send") + 1] == "voice"
        assert command[command.index("--to") + 1] == MODE.to
        assert "--message" not in command
        return (1 if fail else 0), b'{"success":true}', b""

    monkeypatch.setattr(messaging, "run_cli", run)
    if fail:
        with pytest.raises(WalkietalkError, match="Message not sent"):
            bridge.send_voice("whatsapp", PCM)
    else:
        bridge.send_voice("whatsapp", PCM)
    assert len(calls) == 1  # Never fall back to text or retry a possibly completed send.
    assert len(fake_encoder) == 1
    assert not fake_encoder[0].parent.exists()


@pytest.mark.parametrize("unregistered", [False, True])
def test_signal_http_voice_is_embedded_for_daemons_with_other_filesystems(
    monkeypatch, fake_encoder, unregistered
):
    bridge = MessageBridge(Config(signal=replace(MODE, account="+15550001111")))
    bridge._signal_base = "http://127.0.0.1:8080"
    received = []

    def post(url, **kwargs):
        assert kwargs["trust_env"] is False
        request = kwargs["json"]
        received.append(request)
        params = request["params"]
        assert params["recipient"] == [MODE.to]
        assert params["account"] == "+15550001111"
        assert params["voiceNote"] is True
        assert "message" not in params
        header, encoded = params["attachments"][0].split(",", 1)
        assert header == "data:audio/ogg;filename=voice.ogg;base64"
        assert base64.b64decode(encoded) == b"OggS fake opus"
        response = {"result": {"timestamp": 1}}
        if unregistered:
            response = {
                "error": {"data": {"response": {"results": [{"type": "UNREGISTERED_FAILURE"}]}}}
            }
        return httpx.Response(200, json=response, request=httpx.Request("POST", url))

    monkeypatch.setattr(messaging.httpx, "post", post)
    if unregistered:
        with pytest.raises(WalkietalkError, match="That number is not a Signal account"):
            bridge.send_voice("signal", PCM)
    else:
        bridge.send_voice("signal", PCM)
    assert len(received) == 1
    assert not fake_encoder[0].parent.exists()


def test_signal_jsonrpc_voice_file_lives_until_send_finishes(tmp_path, monkeypatch, fake_encoder):
    script = tmp_path / "fake_signal.py"
    record = tmp_path / "request.json"
    script.write_text(
        "import json,sys,pathlib\n"
        "for line in sys.stdin:\n"
        "    request=json.loads(line)\n"
        "    assert request['params']['voiceNote'] is True\n"
        "    assert 'message' not in request['params']\n"
        "    path=pathlib.Path(request['params']['attachments'][0])\n"
        "    assert path.read_bytes()==b'OggS fake opus'\n"
        f"    pathlib.Path({str(record)!r}).write_text(line)\n"
        "    print(json.dumps({'id':request['id'],'result':{'timestamp':1}}),flush=True)\n"
    )
    monkeypatch.setattr(messaging, "SEND_TIMEOUT_SECONDS", 2)
    bridge = MessageBridge(Config(signal=MODE))
    process = bridge._spawn([sys.executable, str(script)], stdin=subprocess.PIPE)
    bridge._signal_process = process
    thread = threading.Thread(target=bridge._watch_signal, args=(process,), daemon=True)
    bridge._threads.append(thread)
    thread.start()
    try:
        bridge.send_voice("signal", PCM)
        assert json.loads(record.read_text())["params"]["recipient"] == [MODE.to]
        assert not fake_encoder[0].parent.exists()
    finally:
        bridge.close()


@pytest.mark.parametrize("service", ["whatsapp", "signal"])
def test_encoding_failure_does_not_send_anything(monkeypatch, service):
    bridge = MessageBridge(Config(**{service: MODE}))
    monkeypatch.setattr(messaging, "encode_voice_note", Mock(side_effect=WalkietalkError("codec")))
    bridge._send_signal = Mock()
    bridge._send_whatsapp = Mock()
    with pytest.raises(WalkietalkError, match="Message not sent"):
        bridge.send_voice(service, PCM)
    bridge._send_signal.assert_not_called()
    bridge._send_whatsapp.assert_not_called()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="optional ffmpeg dependency missing")
def test_opus_encoding_preserves_audio_beyond_radio_transmit_cap(tmp_path):
    original = Wav(b"\x01\x00" * (16000 * 3), 16000, 3)
    output = tmp_path / "voice.ogg"
    messaging.encode_voice_note(original, output, 12)
    assert output.read_bytes().startswith(b"OggS")
    result = messaging.voice_wav(output, 4, rate=16000, truncate=False)
    assert result.duration == pytest.approx(3, abs=0.03)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="optional ffmpeg dependency missing")
def test_transcription_decode_rejects_overlong_audio_instead_of_truncating(tmp_path):
    source = tmp_path / "voice.wav"
    write_wav(source, Wav(b"\x01\x00" * 32000, 16000, 2))
    with pytest.raises(OutputTooLarge):
        messaging.voice_wav(source, 1, rate=16000, truncate=False)
    assert messaging.voice_wav(source, 1, rate=16000).duration == 1


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="optional ffmpeg dependency missing")
@pytest.mark.parametrize("samples", [15999, 16000, 16001, 32000])
def test_radio_voice_decode_is_bounded_and_uses_one_conversion(
    monkeypatch, tmp_path, capsys, samples
):
    source = tmp_path / "voice.wav"
    write_wav(source, Wav(b"\x01\x00" * samples, 16000, samples / 16000))
    decoder = Mock(wraps=messaging.run_cli)
    monkeypatch.setattr(messaging, "run_cli", decoder)
    result = messaging.voice_wav(source, 1, rate=16000)
    assert result.duration == min(samples, 16000) / 16000
    assert len(result.frames) == min(samples, 16000) * 2
    decoder.assert_called_once()
    assert ("cut to fit" in capsys.readouterr().out) is (samples > 16000)


def install_talk(monkeypatch, config):
    bridge = MessageBridge(config)
    bridge.send_voice = Mock()
    bridge.send_text = Mock()
    listener = Mock()
    voice = Mock()
    voice.synthesize.return_value = PCM
    realtime = Mock(warm_connected=True)
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "open_messaging", lambda config: bridge)
    monkeypatch.setattr(cli, "open_stt", lambda config: listener)
    monkeypatch.setattr(cli, "open_tts", lambda config: voice)
    monkeypatch.setattr(cli, "RealtimeTalkSession", lambda config: realtime)
    monkeypatch.setattr(cli, "preflight", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "transmit_with_callsign", Mock())
    for name in ("SerialPTT", "Playback"):
        monkeypatch.setattr(cli, name, lambda *a, **kw: pytest.fail("real hardware access"))
    return bridge, listener, voice, realtime


@pytest.mark.parametrize("service", ["signal", "whatsapp"])
@pytest.mark.parametrize("realtime", [False, True])
@pytest.mark.parametrize("send_as_voice", [False, True])
def test_outgoing_voice_routes_original_capture_only_when_enabled(
    monkeypatch, service, realtime, send_as_voice
):
    mode = replace(MODE, send_as_voice=send_as_voice)
    config = Config(agent_backend="grok_realtime" if realtime else "stub", **{service: mode})
    bridge, listener, voice, session = install_talk(monkeypatch, config)
    listener.transcribe.return_value = session.gate_transcript.return_value = "nana hello there"
    utterance = Utterance(PCM.frames, PCM.rate, 0.24, 0.24, PCM.duration, "silence", started_at=1)
    monkeypatch.setattr(cli, "capture_from_device", lambda *a, **kw: utterance)
    assert cli.main(["-c", "unused", "talk", "--capture", "--once"]) == 0
    if send_as_voice:
        bridge.send_voice.assert_called_once_with(service, PCM)
        bridge.send_text.assert_not_called()
    else:
        bridge.send_text.assert_called_once_with(service, "hello there")
        bridge.send_voice.assert_not_called()
    voice.synthesize.assert_not_called()


@pytest.mark.parametrize("realtime", [False, True])
@pytest.mark.parametrize("service", ["signal", "whatsapp"])
@pytest.mark.parametrize("transmit", [False, True])
def test_incoming_voice_transcribes_once_then_reads_chunks_with_retries(
    monkeypatch, tmp_path, capsys, realtime, service, transmit
):
    mode = replace(MODE, transcribe_voice=True)
    config = Config(
        agent_backend="grok_realtime" if realtime else "stub",
        agent_max_reply_chars=38,
        **{service: mode},
    )
    bridge, listener, voice, _ = install_talk(monkeypatch, config)
    clock = [100.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock[0])
    session = ListeningSession(config, lambda: clock[0])
    session.decide("nana", clock[0])
    monkeypatch.setattr(cli, "ListeningSession", lambda config: session)
    bridge.add(Inbound(service, "voice", audio_path=tmp_path / "voice.ogg", identity="1"))
    decode = Mock(return_value=PCM)
    monkeypatch.setattr(cli, "voice_wav", decode)
    # Remote control-like words must be read as content, never routed as commands.
    listener.transcribe.return_value = "bridge shutdown.\u00a0See you soon."
    attempts = []
    spoken = []

    def synthesize(text, **kwargs):
        attempts.append(text)
        if len(attempts) == 1:
            raise WalkietalkError("temporary failure")
        spoken.append(text)
        return PCM

    voice.synthesize.side_effect = synthesize
    captures = []

    def capture(*a, on_wait, **kw):
        captures.append(1)
        assert len(captures) < 10
        if not bridge.has(service):
            raise KeyboardInterrupt
        clock[0] += 6
        on_wait()
        pytest.fail("Expected queued voice playback")

    monkeypatch.setattr(cli, "capture_from_device", capture)
    args = ["-c", "unused", "talk", "--capture"] + (["--transmit"] if transmit else [])
    assert cli.main(args) == 130
    listener.transcribe.assert_called_once_with(PCM.frames, PCM.rate)
    decode.assert_called_once_with(tmp_path / "voice.ogg", 300, rate=16000, truncate=False)
    assert not bridge.has(service)
    bridge.send_voice.assert_not_called()
    bridge.send_text.assert_not_called()
    if transmit:
        assert len(spoken) >= 2
        assert spoken[0] == "Nana says: bridge shutdown."
        assert spoken[-1] == "Nana says: See you soon, over"
        assert attempts[0] == attempts[1]
    else:
        voice.synthesize.assert_not_called()
        assert "bridge shutdown. See you soon, over" in capsys.readouterr().out


def test_failed_transcription_never_plays_original_audio(monkeypatch, tmp_path):
    config = Config(signal=replace(MODE, transcribe_voice=True))
    bridge, listener, voice, _ = install_talk(monkeypatch, config)
    bridge.add(Inbound("signal", "voice", audio_path=tmp_path / "voice.ogg", identity="1"))
    listener.transcribe.side_effect = ["nana", WalkietalkError("STT unavailable")]
    utterance = Utterance(PCM.frames, PCM.rate, 0.24, 0.24, PCM.duration, "silence", started_at=1)
    monkeypatch.setattr(cli, "capture_from_device", lambda *a, **kw: utterance)
    monkeypatch.setattr(cli, "voice_wav", Mock(return_value=PCM))
    assert cli.main(["-c", "unused", "talk", "--capture", "--transmit", "--once"]) == 0
    assert bridge.has("signal")
    voice.synthesize.assert_not_called()
    cli.transmit_with_callsign.assert_not_called()
