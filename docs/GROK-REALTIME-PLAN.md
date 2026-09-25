# Grok realtime speech-to-speech

Select `agent.backend: grok_realtime` to stream captured PCM audio directly to
xAI's Speech to Speech WebSocket. This is an explicit billed API backend using
`agent.realtime.api_key_env` (default `XAI_API_KEY`), not a SuperGrok login.
The independent STT, text-agent, and TTS backends remain available for other
agent selections. Realtime `talk` does not open them.

The original PR review and its regression evidence are recorded in
[PR-16-REVIEW.md](PR-16-REVIEW.md).

## Turn flow

1. Capture/VAD sends audio frames into a warm WebSocket as they arrive.
2. At the end of capture, Walkietalk commits the audio. On every turn, including
   conversation follow-ups, it waits for this same session's input
   transcript before deciding whether to request a reply. It never sends that
   transcript back as a substitute text question.
3. A rejected, wake-only, or shutdown-control item is deleted before any
   `response.create`. Failed/missing transcripts discard the session and fail
   closed. Empty follow-ups are ignored, and a wake phrase alone receives only
   the configured acknowledgement, even when shutdown controls are disabled.
4. Output PCM chunks feed an isolated audio worker incrementally. Device
   preparation happens with PTT off; the first audible chunk keys PTT, waits the
   configured radio settle interval, and enters the playback stream. Only
   preceding quiet audio is limited to the 150 ms pre-roll window.
5. Audio completion or a tool gap drains queued playback before releasing PTT.
   A separate watchdog enforces the total TX airtime budget even if the network,
   event loop, or audio worker stalls. Cancellation releases PTT and stops audio.
6. A configured station ID uses a separate bounded realtime voice burst after
   the reply. Interval IDs are marked only after successful transmission.

Wake, shutdown, and empty-input detection require a native transcript; they do not
invoke a separate transcription service or local speech model. VAD hangover,
model processing, network delay, and radio settle time still affect latency.

Accepted audio retains the spoken wake phrase. Realtime `talk` automatically
adds response guidance using `wake.primary` and `wake.aliases`: treat a leading
wake phrase as a bridge routing prefix and answer as if the prefix were removed.
The wake configuration does not assign the model a name, identity, or persona.
Identity questions use the model's established identity or an explicitly
configured persona. Explicit questions about wake phrases remain ordinary
questions. This guidance accompanies both default and
custom `agent.instructions`; no duplicate wake-name configuration is needed.

Silent carriers and radio noise can pass the energy detector without yielding
words. The input transcript gate has a separate five-second deadline (or the
configured idle timeout if shorter), covering commit and transcript arrival.
Pings do not extend it. On expiry the turn is discarded without a reply and
continuous mode resumes listening with its existing window deadline unchanged,
using the same `Ignored (empty transcript). Window unchanged.` message as other
modes. An explicitly empty transcript is
also ignored in an open conversation, regardless of shutdown settings.

## Recovery and conversation state

Healthy turns reuse the voice session and its conversation. On response failure,
TX truncation, or cancellation, Walkietalk attempts `response.cancel` and closes
the socket. It deliberately discards that conversation rather than retaining
unheard speech or accidentally processing leftover response events. The next
utterance reconnects with the configured instructions and starts fresh.

Completed turns are bounded by `agent.history_turns`. Older user, assistant,
and tool items are deleted together with acknowledgements matched by item ID.
If IDs are unavailable or pruning fails, the next turn starts a fresh session.
Replies that never produce audible output do not open a follow-up window or
trigger a station ID. Truncated replies also leave the follow-up window closed.
Offline checks require a successful `response.done` before saving reply audio.

Upload and transcript failures discard all partial input. A network failure
while capturing abandons that recording and returns to wake listening. PTT
control failures stop the command after cleanup rather than retrying hardware.
A healthy socket stays open while `talk` is running. A background reader drains
events between turns, ignores application pings, and detects disconnects. Its
queue is bounded; overflow discards the session rather than losing events silently.
Continuous mode retries failed connections with a one-second delay. Failed
cleanup of rejected input resets the session and permits the next listen.
The conversation follow-up window controls acceptance, not socket lifetime.

## Billing

xAI's [Speech to Speech model page](https://docs.x.ai/developers/models/speech-to-speech),
checked September 25, 2026, specifies $0.08 per minute of audio sent or received.
It also specifies $0.004 per client `conversation.item.create` event, except
tool results and audio-content items. `response.create` itself has no event fee;
generated audio is metered. Fixed phrases use `force_message` via
`conversation.item.create`, so they incur the text-event fee plus output audio.

The documented meter is audio duration, not idle WebSocket connection time.
The earlier review's $115/day idle-connection scenario is therefore unsupported
by the published billing model. VAD-selected chatter uploaded before wake
validation can still count as sent audio, even when the turn is rejected.
These are documented rates, not a measurement of this account's invoice.

## Configuration

`agent.realtime` and its individual fields are optional; omitted values use the
shown defaults. Older configurations for other backends continue to load.

```yaml
agent:
  backend: grok_realtime
  realtime:
    model: grok-voice-latest
    voice: eve
    api_key_env: XAI_API_KEY
    websocket_url: wss://api.x.ai/v1/realtime
    connect_timeout_seconds: 10
    idle_timeout_seconds: 60
```

This fragment belongs inside a complete Walkietalk configuration. Existing
`wake`, `shutdown`, `listening`, and radio duration/callsign settings still apply.
`agent.web_search` enables the realtime session's native web-search tool.

## Verification

Automated tests use fake transports, fake PTT, and isolated subprocesses with no
audio hardware. They cover streaming before completion, preservation of the
first chunk, native wake/shutdown gates, failure recovery, watchdog deadlines,
cancellation during drain, station IDs, and configuration compatibility.

No live xAI request or on-air transmission was performed during the PR review
fixes. RF quality and actual end-of-utterance-to-first-audio latency remain to be
measured on the operator's AIOC setup.

Operator checks (each realtime request uses API credits):

- `voice-agent-check UTTERANCE.wav --output reply.wav`: capture-file input and
  saved reply audio, with no radio hardware.
- Add `--supervised` for fake PTT; this does not measure physical playback timing.
- `talk --capture`: native wake/control handling and streamed audio input with
  printed replies; no radio output.
- `talk --capture --transmit`: actual streaming radio playback. Measure the gap
  from end of received speech to the first audible reply, confirm first words
  are intact, test station identification, and verify release on interruption.

All hardware commands require an explicit `-c CONFIG`. Live API spending and
on-air testing remain operator-authorized checks, not CI work.

## References

- [Speech to Speech](https://docs.x.ai/developers/model-capabilities/audio/speech-to-speech)
- [Voice protocol](https://docs.x.ai/developers/rest-api-reference/inference/voice)
- [Backend setup](BACKENDS.md)
- [Configuration](CONFIGURATION.md)
