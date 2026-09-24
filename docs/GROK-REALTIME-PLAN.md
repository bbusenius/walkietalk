# Grok Voice speech-to-speech plan (not implemented)

**Status: plan only.** Walkietalk does not implement a Grok Speech to Speech /
`/v1/realtime` adapter yet. This document records the intended architecture so a
later teaching phase can add it without changing radio ownership or silently
replacing existing backends.

This optional path would **not** replace:

| Existing selection | Role today |
| --- | --- |
| `stt.backend: grok` / `grok_api` | Voice Transcribe (`POST /v1/stt`) |
| `agent.backend: grok` | Grok Build CLI text replies |
| `tts.backend: grok` / `grok_api` | Voice API (`POST /v1/tts`) |

Those adapters stay independent. Choosing realtime later must be an explicit
operator selection, not a fallback when STT, agent, or TTS fails.

## Motivation

Today a spoken radio turn is serial: capture → STT → wake/agent gates → TTS →
parent-owned PTT playback. That is clear and testable, but time-to-first-spoken-
word includes the full STT and agent round trips before any audio leaves the
gateway.

xAI's Speech to Speech API streams recognition, reasoning, and speech over one
realtime WebSocket (`wss://api.x.ai/v1/realtime`). Used carefully, that can
shorten the gap between unkey and the first TX audio chunk.

### Empirical note (2026-09-24)

A live walkie-talkie / call test on Thursday 2026-09-24 found realtime
speech-to-speech **noticeably faster** than the current Walkietalk
STT→agent→TTS chain. Earlier analysis treated a shorter time-to-first-word as a
reasonable expectation; the live test showed that the round-trip through
separate STT and TTS stages cost more than modeled. The latency benefit is
empirical, not only theoretical — but this document remains plan-only; no
realtime adapter is implemented yet.

Total TX airtime still needs the same discipline as today: short-answer agent
instructions, existing TX duration caps, and parent unkey. A faster first word
does not authorize longer transmissions.

## PTT and half-duplex

Push-to-talk simplex is not a blocker. Phone-style barge-in does not apply: the
operator cannot interrupt TX on the same channel while the gateway is keyed.

Map radio events to realtime turns roughly as follows:

1. Operator keys, speaks, and unkeys (end of RX utterance).
2. Walkietalk treats that unkey / end-of-utterance as the user-turn commit into
   the realtime session (after the wake gate allows the turn).
3. When the session emits AI audio, the **parent** keys PTT, plays bounded audio,
   then unkeys — the same ownership rule as every other TTS path.
4. Idle conversation windows and session teardown stay under Walkietalk control;
   the WebSocket must not keep the radio keyed.

Do not embed serial, PortAudio, or PTT inside the realtime client. Radio glue
stays unchanged.

## Adapter shape

Add a new combined **voice agent** backend kind (name TBD in a later schema PR),
not a silent substitute stuffed into the existing `stt` / `agent` / `tts` slots.

| Concern | Expected owner |
| --- | --- |
| Wake / shutdown phrases | Existing gates; realtime audio in only after wake allows |
| Conversation idle timeout | Walkietalk session policy |
| WebSocket open/close and streaming | New voice-agent adapter |
| PCM validation, gain, TX caps, unkey | Parent / existing radio path |

Keep selecting independent STT, agent, and TTS backends for operators who want
the current serial path. Realtime is an alternate combined route when explicitly
configured.

## Billing and authentication

Expect explicit developer API credentials for Speech to Speech:

- `XAI_API_KEY` (or an equivalent named env field decided in a schema PR)
- Console API credits; Speech to Speech is billed separately from text models
  (about **$0.08 per minute** of audio at the published Voice pricing table —
  confirm against current docs before any live check)

**SuperGrok Plus** powers the existing account-login `grok` STT / Build / TTS
adapters. That entitlement is separate from embedding `/v1/realtime` in
Walkietalk. Never auto-switch a subscription login to API billing; that remains
an existing project rule for every Grok path.

Live API spend belongs to supervised operator checks, not CI.

## Hard parts

These need design attention before claiming the path is ready:

- **RF audio quality.** Walkie capture is noisy and band-limited compared with
  headset demos; VAD and recognition may need different thresholds or offline
  capture tests first.
- **Wake vs long-lived session.** A WebSocket held open for conversation must
  still respect wake addressing and idle timeout; do not leave a billed session
  running after the operator walks away.
- **Long answers vs TX discipline.** Streaming speech can outrun radio caps;
  truncate or stop synthesis when the parent hits the TX limit, then unkey.
- **Tool-call pauses.** If the session pauses audio while calling tools, unkey
  during the gap and re-key only when spoken audio resumes — avoid dead air with
  PTT held.
- **Cost of an open WebSocket.** Idle open time still costs; close promptly when
  the conversation window expires or talk mode exits.

## Optional hybrid later

A later phase could keep **local STT for wake only**, then hand the conversational
core to realtime after the wake phrase matches. That preserves offline wake
checks and limits billed realtime minutes to addressed turns. It is optional and
not required for the first realtime adapter.

## Official references

- [Voice / Speech to Speech](https://docs.x.ai/developers/model-capabilities/audio/voice)
- [Models and pricing](https://docs.x.ai/docs/models#pricing)
- [Billing](https://docs.x.ai/docs/key-information/billing)

Also see the implemented Grok paths: [Grok agent](GROK.md),
[Grok voice](GROK-TTS.md), and [Backend setup](BACKENDS.md).

## Out of scope for the plan PR

This documentation change does **not**:

- implement adapter code or WebSocket clients
- add config schema fields or example YAML keys
- run live `/v1/realtime` API checks
- perform on-air testing

## Suggested future implementation phases

Follow the existing one-phase-per-PR teaching workflow:

- [ ] **Schema + fake WebSocket tests** — explicit backend selection, auth env
      name, failure contract, and automated fake-transport coverage with no live
      credits
- [ ] **Offline capture path** — file/capture in → streamed events → WAV or
      printed transcript/reply out; TX remains off
- [ ] **Supervised TX** — parent-owned PTT playback with duration caps, unkey on
      tool pauses and errors, and a recorded live demonstration checklist

Passing fake tests alone is not completion of a later phase. Record live checks
separately before claiming the integration is verified.
