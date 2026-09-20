# Agent and speech-listener capabilities

An agent receives text after the wake gate; an STT adapter receives captured
audio and returns a transcript before that gate. Supporting an agent does not
automatically make its voice interface usable as an independent listener.

The phase 5 investigation checked Charlotte's installed Hermes 0.19.0 source,
her selected STT provider, Codex CLI 0.155.1 help and generated protocol schema,
and the official documentation linked below. No audio was uploaded and no
billable API request was made during this investigation.

| Integration | Agent | STT status |
| --- | --- | --- |
| Local faster-whisper | Not an agent | Implemented and family-tested; `tiny` or `base` |
| Grok Build / Grok Voice Transcribe | Build CLI implemented and family-tested | Voice Transcribe implemented and family-tested using SuperGrok login; separate service from Build |
| Grok STT API | No direct chat API adapter | Explicit `grok_api` with `XAI_API_KEY`; automated checks pass, live key demo skipped |
| Charlotte/Hermes | Implemented and family-tested | Hermes has internal STT, but its installed API does not expose transcription; Brad chose to keep Walkietalk's local Whisper option instead of adding a service |
| Codex | Implemented and family-tested | No standalone transcription interface found in the inspected CLI/protocol; not implemented or advertised as supported |
| Claude Code / Messages API | Both implemented; coding assistant verified CLI text turns, and Brad confirms both routes work as expected | No documented standalone transcription contract found in the inspected CLI or Messages API; not implemented for STT |

## Hermes findings

Charlotte's current profile selects `stt.provider: local` with local model
`base`. Hermes implements `tools.transcription_tools.transcribe_audio()` for
incoming voice messages, and that helper can use several configured engines.
See [Hermes voice transcription](https://hermes-agent.nousresearch.com/docs/user-guide/features/tts#voice-message-transcription-stt).

The inspected `gateway/platforms/api_server.py` route table has no transcription
endpoint; its capabilities explicitly report `audio_api: false` and
`realtime_voice: false`. The existing agent Runs API accepts text. The
[API server documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server)
does not establish a supported transcription endpoint for this installation.

Exposing the helper would require a new transcription-only service or a future
upstream endpoint. Brad explicitly chose to keep using Walkietalk's existing
local Whisper option for now. No Hermes runtime changes or restart were made.
This does not rename local Whisper to `hermes` or send audio through Charlotte's
agent and ask her to produce a transcript.

## Codex findings

The installed CLI has no standalone transcription subcommand or audio-file
option for `exec`. Its generated experimental schema exposes
`thread/realtime/start`, `appendAudio`, `appendText`, `appendSpeech`, `stop`,
and `listVoices` under that thread/realtime namespace. These are live
conversation operations; the schema does not expose a separate transcription
request. Audio or transcript notifications alone do not establish a supported
STT-only contract that preserves Walkietalk's wake-before-agent behavior.

The [official app-server documentation](https://learn.chatgpt.com/docs/app-server)
describes integration and schema generation. To repeat the read-only interface
inspection without starting an agent session:

```sh
codex --version
codex --help
codex exec --help
codex app-server generate-json-schema --experimental --out /tmp/walkietalk-codex-stt-schema
```

This is a version-specific finding, not a claim that Codex has no voice features
or that future STT integration is impossible. Codex STT remains unavailable in
Walkietalk pending a verified transcription-only interface. We have not made a
live transcription attempt, copied account tokens, or called undocumented
ChatGPT endpoints.

OpenAI separately offers a documented
[file transcription API](https://developers.openai.com/api/docs/guides/speech-to-text).
That would be an explicitly selected, API-authenticated integration with its
own billing, not a `codex` saved-login adapter. It has not been added or chosen
as a substitute.

## Current configuration

The available STT selections remain `faster-whisper`, `grok`, and `grok_api`.
Any of them can supply text to any implemented agent. Neither `stt.backend:
hermes` nor `stt.backend: codex` is accepted. Claude selections also remain invalid
under `stt.backend`. No fallback occurs.

The capability review leaves Brad's active local configuration unchanged.
Choosing to keep the existing Whisper option does not silently switch a config
currently using Grok STT. To deliberately choose local recognition, edit only
`stt.backend` to `faster-whisper`, then restart the bridge.

## Claude findings

Brad subsequently requested both Claude agent routes and STT if available.
Claude Code **2.1.277** help and the current official
[CLI reference](https://code.claude.com/docs/en/cli-reference) expose text input
for print mode, with no documented audio-file transcription contract.
The [Messages API](https://platform.claude.com/docs/en/api/messages/create) and
[model catalog](https://platform.claude.com/docs/en/models/overview) document
text/image inputs, not a standalone audio transcription endpoint. Consumer voice
features do not establish an STT endpoint for this bridge.

No supported Claude STT adapter was identified, so none is advertised or aliased
to another service. This is an interface finding, not a claim that future support
is impossible. `claude` and `claude_api` accept text from any of Walkietalk's
existing listeners. See [Claude agent setup](CLAUDE.md). No audio was sent to
Claude during this investigation. Previously accepted phase 5 demos remain accepted.
