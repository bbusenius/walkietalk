# WhatsApp and Signal contacts

This feature adds optional messaging conversations. The agent, and the wake phrase that opens
the agent, stay as they are.

A person on a walkie can hold a conversation with a WhatsApp or Signal
contact, with no screen. They talk. Walkietalk sends their words to that
contact. The contact reads and replies on their phone. Walkietalk transmits
the reply on the radio.

The contact is reached with [wacli](https://github.com/openclaw/wacli) for
WhatsApp and with [signal-cli](https://github.com/AsamK/signal-cli) for Signal.
Signal uses a `signal-cli` daemon that is already running on this computer when
one is answering at `http://127.0.0.1:8080`. A second `signal-cli` process
waits on the account lock, so send and receive stay on that one daemon.
Incoming messages come from its event stream. If no daemon is running,
Walkietalk starts one `signal-cli jsonRpc` process and uses it for both.
The feature is optional and configured. With no contacts configured, the radio
behaves as it does today.

`messaging.signal.to` is the recipient's number. Set `messaging.signal.account`
to the local sending account's number in `+countrycode` format when signal-cli
has more than one account registered. Walkietalk uses that one account for both
sending and receiving, through either the existing HTTP daemon or its own
jsonRpc process. Leave `account` empty (or omit it) when only one local account
is registered. An existing daemon must serve the selected account.

Signal attachments default to `$XDG_DATA_HOME/signal-cli/attachments`, or
`~/.local/share/signal-cli/attachments` when that variable is unset. Set
`messaging.signal.attachments_dir` to an absolute path (or a path starting with
`~/`) when an existing daemon uses a different directory, such as one selected
with `signal-cli --config`. The directory must be readable by Walkietalk; for a
container, use the host path to the shared attachment directory. Missing files
are retried for up to 60 seconds without blocking Signal sends or receives,
then reported locally if still unavailable.

Before enabling messaging, install its
[optional external dependencies](INSTALL.md#optional-messaging-dependencies):
`wacli` for WhatsApp, `signal-cli` for Signal, and `ffmpeg` for incoming audio
playback/transcription and outgoing voice-note encoding. Walkietalk's pip installation does not install these programs.

WhatsApp uses `WACLI_STORE_DIR` when set; otherwise it reuses `~/.wacli` if
present, or uses `$XDG_STATE_HOME/wacli` (default `~/.local/state/wacli`) on
Linux. Sync, sending, and incoming-message polling all use that same store.
Existing WhatsApp history is ignored at startup, including older messages that
sync into the database later. Only messages timestamped at or after bridge
startup are eligible. Queues are not durable across restarts. Voice downloads
get up to 60 seconds from their first observation, independent of send time.

## Wake phrases

The existing wake phrase opens the AI agent. It is not a name, and it is not
part of a message to a contact.

WhatsApp and Signal each have their own wake phrase. Speech recognition already
decides whether a transmission starts with the agent wake phrase. The same
check recognizes a messaging wake phrase. The longer phrase wins when one
configured phrase is a prefix of another.

The radio is in one conversation at a time. A messaging wake phrase puts the
agent to sleep and opens that contact. The agent wake phrase closes the contact
and opens the agent. The sleep phrase closes whichever conversation is open.
Shutdown still wins before any of those phrases.

Each service has its own `listening.mode` and `listening.conversation_timeout_seconds`
under `messaging.whatsapp` or `messaging.signal`, independent of the agent.
Messaging defaults to `conversation` with a 60-second follow-up window;
the timeout accepts values greater than zero through 600 seconds.
In `wake_phrase` mode the phrase is required for each outgoing message.
A wake phrase alone activates the contact without sending an empty message.

The follow-up timeout stops unprefixed outgoing messages, but the active contact
can still reply. Explicit sleep holds incoming replies and stops unprefixed
sending to both the agent and contacts. Switching to another destination holds
the previous contact's replies until its wake phrase is used again.

By default, speech after the messaging phrase is the text that is sent. The phrase
itself is removed from text messages.

## Optional voice conversion

Each endpoint supports two independent switches, both `false` by default. Add them
to the existing `messaging.whatsapp` or `messaging.signal` settings:

```yaml
messaging:
  whatsapp:
    wake: "nana"
    aliases: []
    to: "+15551234567"
    empty_queue_phrase: ""
    send_as_voice: true
    transcribe_voice: true
  signal:
    wake: "grandma"
    aliases: []
    to: "+15557654321"
    empty_queue_phrase: ""
    send_as_voice: false
    transcribe_voice: true
```

`send_as_voice: true` sends the original captured radio audio as a voice note,
instead of sending its transcript. The recording includes the wake phrase if it
was spoken; unprefixed follow-ups contain only the captured follow-up. Recognition
still determines the destination and handles sleep/shutdown before anything is
sent. A wake phrase alone sends nothing. The capture limit
(`vad.max_utterance_seconds`) still applies; the radio's outbound transmit cap
does not shorten an outgoing note. TTS is not used for these uploads. Temporary
audio files are removed after the send attempt, and a failed voice send never
falls back to a text send.

Voice uploads require ffmpeg with the `libopus` encoder. WhatsApp uses
[wacli's `send voice` command](https://github.com/openclaw/wacli/blob/main/docs/send.md).
Signal requires a version of signal-cli with `--voice-note` support; its
[JSON-RPC interface](https://github.com/AsamK/signal-cli/blob/master/man/signal-cli-jsonrpc.5.adoc)
uses `voiceNote` and `attachments`. For an existing HTTP daemon, the audio is
embedded in the request, so it need not share Walkietalk's temporary directory.

`transcribe_voice: true` converts incoming voice notes to text using `stt.backend`,
then reads that text with `tts.backend`, including the usual sender introduction
and final “over.” The full transcript uses the same chunking, retry, and queue
progress as a text reply. A successful transcript is reused across playback
retries and chunks. Transcription failures retry through the existing queue
policy; they never fall back to playing the original recording. In receive-only
mode, the transcript is displayed without transmitting audio.

This also works with `agent.backend: grok_realtime`: the explicitly configured
STT backend is prepared for incoming notes, while realtime still handles radio
speech. Notes up to five minutes can be transcribed; longer notes are rejected
and reported rather than silently shortened. The configured STT timeout still
applies. A remote STT backend receives the contact's audio for transcription.
Transcripts are message content and are never interpreted as wake, sleep, shutdown,
or agent instructions.

## Queue

Replies wait in memory while that conversation is asleep. While it is the open
conversation, a reply is transmitted when the radio is idle. Messages do not
survive app restart. The app plays one message, then resumes capture with a
one-second listening opportunity before another queued reply or text chunk. Preparation failures
retain the current chunk and retry after five seconds; after three failed attempts,
the unplayable remainder is skipped with an error so later messages can proceed. Successfully spoken replies
renew the active contact's conversation follow-up window. Queued playback pauses
while remote shutdown awaits its confirmation code. Already queued contact
replies can play between realtime reconnection attempts during an xAI outage;
new radio speech still needs the configured realtime transcription service.

Only incoming messages from the configured contact are eligible. Signal outgoing
sent-message sync copies from linked devices are ignored, including self-number
testing copies.

Using the messaging wake phrase by itself speaks whatever is waiting. If
nothing is waiting, the radio speaks that mode's `empty_queue_phrase`. A blank
phrase stays silent, the same way an empty agent wake confirmation stays
silent. If speech follows the phrase, that text is sent, the empty-queue
phrase is not spoken, and anything already waiting is spoken after the send.

## Replies

Every reply is transmitted on the radio with PTT, under the same transmit
limit, settle, and post-transmit mute as any other spoken reply. Station ID
uses the callsign already configured. When an ID is due it is appended, or sent
as the next burst if it does not fit. With station ID off, a reply does not
carry one.

Each reply is introduced with the configured `sender_alias`, never a contact-book
name or number. A blank alias uses the configured messaging wake phrase.
When messaging and `--transmit` are enabled, the configured TTS provider is
prepared at startup, including with `grok_realtime`. Missing TTS setup stops
startup before the bridge opens. Incoming text has terminal controls and invisible
format characters removed and Unicode whitespace converted to ordinary spaces.
A text reply is spoken with the configured TTS provider as "Alias says: message,
over". Long text is split at sentence or word boundaries into as many transmissions
as needed, without summarization or truncation. Each chunk includes the sender
introduction; only the final chunk adds "over". Chunks fit the TTS character limit
and are shortened further if their synthesized audio exceeds the transmit limit.
Initial sizing estimates the available speaking time; subsequent chunks use the
measured speech rate. A timeout or an older Hermes service's generic size failure
reduces the next retry's chunk size.
Completed chunks are not repeated after a retry, sleep, or conversation switch.
If using Hermes TTS, update the deployed speech service script along with
Walkietalk so it can report overlong speech distinctly for automatic splitting.

With `transcribe_voice: false`, a voice reply has a TTS alias introduction followed
by the original audio. Both
fit within the existing transmit limit; overlong audio is truncated. The audio
file is converted to the radio's 48 kHz mono PCM16 WAV. It is not passed through TTS, and "over" is not added to it. The
message type and the endpoint's `transcribe_voice` setting decide how it is played.

A failed outgoing send is reported locally and, with `--transmit`, spoken on the
radio. Continuous mode keeps listening; `--once` and WAV input exit with a failure
status. Messages are not automatically resent.

Walkietalk handles stored messages. It does not place or bridge live voice calls.

## Regulatory information

This guide describes software behavior, not legal advice or a determination that
any particular use is permitted. Before transmitting, review the requirements
for your radio service and jurisdiction. For US GMRS, see the
[FCC's licensing and operating information](https://www.fcc.gov/wireless/bureau-divisions/mobility-division/general-mobile-radio-service-gmrs)
and [47 CFR Part 95, Subpart E](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-D/part-95/subpart-E).
