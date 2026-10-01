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

With operator mode off, the follow-up timeout stops unprefixed outgoing messages, but the active contact
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

With operator mode off, using the messaging wake phrase by itself speaks whatever is waiting. If
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

A failed outgoing send with operator mode off is reported locally and, with `--transmit`, spoken on the
radio. Continuous mode keeps listening; `--once` and WAV input exit with a failure
status. Messages are not automatically resent.

Walkietalk handles stored messages. It does not place or bridge live voice calls.

## Operator mode

Set one optional switch beside the service blocks in your existing config:

```yaml
messaging:
  operator_mode: true
  whatsapp:
    # your existing settings
  signal:
    # your existing settings
```

The default is `false`; omitting it keeps automatic messaging. When enabled,
incoming and outgoing messages wait in one review queue, oldest first, across
both services. Run continuous `talk --capture`, adding `--transmit` for radio
delivery. WAV input and `--once` are refused. The running radio terminal shows
logs; operator commands run separately, on the same computer and as the same
user. They find the running instance automatically:

```bash
# Radio terminal:
walkietalk -c config.local.yaml talk --capture --transmit

# A second terminal:
walkietalk operator status
walkietalk operator read
walkietalk operator approve
# Or approve and release this incoming message without a radio wake:
walkietalk operator transmit
# Or discard the item:
walkietalk operator deny

# Review and release an incoming message that was approved earlier:
walkietalk operator status --approved
walkietalk operator transmit --approved
# Put the conversation to sleep without saying the radio phrase:
walkietalk operator sleep
```

`operator` without an action defaults to `status`. Message commands print the current
review head. `--approved` selects the oldest approved incoming message across both
services instead; it supports status, read, transmit, and deny. Actions stay bound
to that item even if the queue changes while the radio is busy. Commands print
progress and return when their action finishes;
they do not need input in the radio terminal.

The operator commands do not require a config argument. You can still pass
`--config` to target a specific running instance; if several are running, the
commands ask you to select one instead of guessing.

For a fixed operator panel in the radio terminal, add `--panel`:

```bash
walkietalk -c config.local.yaml talk --capture --transmit --panel
```

The upper area shows live radio logs; the lower area stays in place with the
conversation state, queue counts, current item, and action progress. Consecutive
RMS readings update one log line. Press **R** to read, **A** to approve, **T** to
approve and transmit an incoming message, **D** to deny, and **Ctrl+C** to stop.
Press **S** to put the conversation to sleep from either view, including when the
queue is empty.
Press **Tab** to switch between review and approved incoming messages. In the
approved view, **T** releases the displayed message; **R** reads it and **D** drops
it. After **A**, use **Tab** to select the approved message before **T**. If the
review view is empty, **T** shows that instruction instead of silently doing
nothing. Read displays a voice transcript; voice approval and transmission
requires an available transcript. When the transcript is already shown, press
**A** directly. Press **R** when the voice item still needs transcription.
Text and cached transcripts wrap; use **Up/Down**, **PageUp/PageDown**, or
**Home/End** to scroll the message. Approval or denial displays the next item.

The Approved view selects the oldest approved message that is first in its
contact's delivery queue. An earlier message awaiting review blocks that
contact's later approvals while other contacts remain available for delivery.

The panel uses the same commands and approval rules as the separate CLI. CLI
commands still work while it is open. Panel actions stay bound to the item on
screen; if another command changes it, the panel reports the change and requires
a fresh action. Shortcuts pause while an action is pending. The panel requires
an interactive terminal of at least 60 columns by 20 rows. If resized smaller,
it pauses shortcuts until enlarged; radio operation continues. Terminal settings
are restored on exit. Omit `--panel` for ordinary logs and separate CLI control.

| Command | Effect |
| --- | --- |
| `operator status` | Show the current message and counts of items waiting for review or approved for radio delivery. |
| `operator read` | Show text or a cached voice transcript with the transmitter unkeyed; keep the item waiting. |
| `operator approve` | Approve an incoming item for radio delivery, or send an outgoing item to its stored contact. Voice requires an available transcript; run `read` if one is not shown yet. |
| `operator transmit` | Approve and schedule one incoming item for delivery without a radio wake. Add `--approved` to release an item approved earlier. Voice requires an available transcript. |
| `operator deny` | Drop the selected item, including any unsent remainder. |
| `operator sleep` | Close the active conversation and pause any pending operator delivery, retaining queued messages and approvals. Works without a selected message. |

Text is shown as each review head appears. The display identifies its direction,
service, and configured contact alias. Commands submitted while capture or
playback is busy wait for the main loop. Queued message commands remain bound to the
displayed item; concurrent approvals cannot approve the next item or retry a
failed send. Canceling a CLI command before execution removes its authorization.
Obtaining a voice transcript changes the displayed content and invalidates an
approval queued before those words were available. Reading a cached transcript
or changing unrelated queued items preserves a command for the unchanged item.

Controls use a private Unix socket under `$XDG_RUNTIME_DIR`, or `/run/user/<uid>`
when available. Headless sessions without either use `~/.cache/walkietalk/operator`.
The path is independent of `TMPDIR` and systemd `PrivateTmp`. Unexpected control
server failure stops `talk` with an unsuccessful exit status.

`operator sleep` is a conversation control and works with either queue empty,
regardless of its displayed item or revision. It clears the selected destination
and follow-up window, pauses any remaining chunks of an operator delivery, and
cancels a pending shutdown confirmation. It keeps queued messages, approvals,
and agent history. The normal sleep confirmation is spoken when configured and
`talk --transmit` is enabled; an empty confirmation stays silent. It also works
when no radio sleep phrase is configured. Like voice sleep, it waits for the
current radio activity to finish, then resumes listening for wake phrases.
An action that has already begun can still finish. The default command wait limit
is 120 seconds; `operator read --timeout 300`, for example, allows a longer wait
(up to 600 seconds). After a timeout or interrupted action, check `operator status`
before retrying.

Review works during sleep and regardless of the selected conversation. Approval
does not wake the radio, change the selected contact, or restart its timer.
An approved incoming item leaves the review queue and waits in its service's
delivery queue, allowing review of later items while the radio is asleep.

With ordinary approval, incoming delivery requires an idle radio and that contact
to be selected. In `conversation` mode it also requires an open follow-up window;
an expired window holds approved replies until the next wake for that contact. Queueing outgoing
traffic and successful incoming playback restart that contact's window. In
`wake_phrase` mode, approved replies use the existing receiving behavior: the
selected contact can reply whenever the radio is idle, without another wake or
a one-message-per-wake restriction. Sleep or switching conversations holds
approved replies in either mode without requiring approval again.

`operator transmit` provides a separate operator-controlled delivery path. It
covers one reviewed incoming message, including its bounded chunks and station
ID, while keeping the selected conversation and its timer unchanged. The bridge
can remain asleep, have another contact selected, or have an expired receive
window. Delivery still waits for idle capture and pauses during shutdown
confirmation. An earlier message in that service's delivery queue must be
reviewed, released, or denied first; the command does not bypass it or release a
backlog. Outgoing messages still use `approve` to send to the stored contact.

The transmit command confirms scheduling; the radio terminal and panel show
delivery progress and completion. Playback failures stay visible in the panel's
action status, with details in the radio log. A playback preparation failure
occurs before PTT is opened for that burst; earlier chunks may already have been
transmitted. Listening resumes between chunks. A new sleep
command or conversation switch cancels the operator delivery request and holds
the remaining chunks as approved. Use `operator transmit --approved` or that
contact's wake to resume. A preparation or playback failure returns the item to
review and cancels its delivery request, requiring a fresh approval or transmit
action. No approval or operator delivery request transfers to the next message.

Outgoing approval sends to the contact selected when the utterance was captured,
even during sleep. The delayed send does not change the current conversation or
its timer. Without `--transmit`, outgoing approvals still send; eligible approved
incoming messages are displayed without radio playback. `operator transmit`
also displays its selected message without opening PTT when the running `talk`
instance omits `--transmit`.

Voice review always displays a transcript in the CLI and panel. It needs no
speaker, plays no audio, and never opens PTT. Failed or empty transcription
keeps the item held; approval remains unavailable until a transcript is available.

For incoming voice, `read` transcribes and caches the full note, up to the
five-minute transcription limit, independently of radio transmit settings.
Read does not generate a sender introduction or call TTS. With
`transcribe_voice: true`, approval speaks that same text with TTS. With
`transcribe_voice: false`, delivery prepares a spoken sender introduction and
the original audio, reporting any cut needed to fit the transmit limit.
Reading a transcript does not change the configured delivery format. The
prepared radio audio stays cached for delivery retries. Receive-only runs
review the same full note without opening TTS.

Incoming review uses the configured `stt.backend`. With `grok_realtime` and raw
voice delivery, that backend is prepared on the first voice read. Repeated reads
reuse the successful transcript. Outgoing voice review reuses the full capture
transcript, including a spoken wake phrase; approval sends the same captured
recording. The capture transcript appears immediately, so outgoing voice can be
approved without running `read` again. Incoming conversion settings do not change
that outgoing payload.

One approval covers the entire incoming message, including bounded text chunks
and any configured station-ID burst. Listening resumes between chunks; sleep,
conversation changes, and the conversation-mode timeout hold the remaining
chunks. A preparation or transmission failure returns the item to review for
another approval or denial; successfully completed chunks are retained. Operator
mode never automatically retries or discards a failed item after three attempts.
After a partial transmission failure, the configured post-transmit mute runs
before capture resumes, and idle message delivery waits another five seconds. A
failure before PTT opens also delays idle delivery for five seconds without muting.
Failure of a due station ID stops `talk`; a completed message burst is committed
before stopping if only its separate ID burst failed.
Outgoing failures also stay held. A send failure can leave delivery uncertain,
and a failed transmission can already have been partly heard, so review the
reported failure before approving another attempt. Pending items do not survive
a restart. A failure to open, key, release, or close PTT stops `talk` after
cleanup; check the radio before restarting.

Operator mode controls message approval. It does not determine whether a use or
payload is permitted under the rules of your radio service. The operator still
must monitor and be able to stop the station whenever it transmits, including
delivery that occurs after approval. A terminal connection does not establish
physical presence at the radio.

## Regulatory information

This guide describes software behavior, not legal advice or a determination that
any particular use is permitted. Before transmitting, review the requirements
for your radio service and jurisdiction. For US GMRS, see the
[FCC's licensing and operating information](https://www.fcc.gov/wireless/bureau-divisions/mobility-division/general-mobile-radio-service-gmrs)
and [47 CFR Part 95, Subpart E](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-D/part-95/subpart-E).
