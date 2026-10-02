# WhatsApp, Signal, and operator mode

A person on a walkie-talkie can trade stored messages with a WhatsApp or
Signal contact, with no screen. They say the contact's wake phrase and their
message; walkietalk sends it. When the contact replies, walkietalk reads the
reply on the radio. It handles stored messages only, never live calls.

Install the tools first ([installation](INSTALL.md#7-messaging-tools-optional)).

```toml
[messaging.whatsapp]
wake = "nana"
aliases = ["nanna"]
to = "+15551234567"
sender_alias = "Nana"
empty_queue_reply = "No new messages, over."

[messaging.signal]
wake = "grandma"
to = "+15557654321"
account = ""            # the local number, if signal-cli has several
```

## Conversations

The radio is in one conversation at a time: the agent or one contact. A
contact's wake phrase opens that contact and closes the agent; the agent's
wake name does the reverse; the sleep phrase closes whichever is open. Remote
shutdown is checked before any of them. Each contact has its own `listening`
mode and follow-up window (default `conversation`, 60 s).

- The contact's wake phrase plus words sends the words (the phrase is removed).
- The wake phrase alone reads what is waiting, or speaks `empty_queue_reply`
  (empty stays silent).
- In `conversation` mode, unaddressed speech within the window goes to the
  open contact.

With `send_as_voice = true` the original recording is sent as an Ogg/Opus
voice note instead of its transcript; the recording includes the wake phrase
if it was spoken in the same transmission. A failed voice send never falls
back to text. Without operator mode, a failed send is reported and, with
`--transmit`, spoken on the radio. Messages are not resent automatically.

## Incoming messages

Only messages from the configured contact count; groups, calls, reactions,
and copies of your own sent messages are ignored. Messages from before startup
are ignored, and queues do not survive a restart.

Without operator mode, the open contact's messages are read whenever the
radio is idle, even after the follow-up window ends. Sleep, or switching to
another conversation, holds them until that contact's wake phrase is used
again. Delivery pauses while shutdown waits for its code.

- Text is read as "Nana says: message, over". Long text is split at sentence
  or word boundaries into as many transmissions as needed, each starting with
  the sender, with "over" only on the last. Nothing is summarized or cut.
  Between pieces, listening resumes for about a second, so you can interrupt
  or say the sleep phrase.
- Voice notes are played after a spoken "Nana says:", cut to fit one
  transmission. With `transcribe_voice = true`, they are transcribed with
  `[stt]` (up to five minutes) and read like text instead.
- Phone text is cleaned: control and invisible characters are removed, and
  all whitespace becomes plain spaces.
- If preparing or sending a message fails, it is retried after five seconds;
  after three failures it is skipped with an error.
- Receive-only `talk` (no `--transmit`) prints messages instead of speaking
  them.

WhatsApp uses `wacli sync --follow` with `WACLI_STORE_DIR`, `~/.wacli`, or
`~/.local/state/wacli`, and reads new messages from its database; voice notes
get a minute to download. Signal uses a signal-cli HTTP daemon already
answering on `127.0.0.1:8080`, or starts `signal-cli jsonRpc` itself (one
process, because signal-cli locks its account). Voice attachments are read from
`attachments_dir` (default `~/.local/share/signal-cli/attachments`) and get a
minute to appear.

## Operator mode

```toml
[messaging]
operator_mode = true
```

Every incoming and outgoing message waits in one review queue, oldest first,
until you approve or deny it. Run continuous `talk --capture` (WAV input and
`--once` are refused), then control it from another terminal as the same user:

```bash
walkietalk operator status
walkietalk operator read        # show text, or transcribe a voice note
walkietalk operator edit        # opens an editor with the current words
walkietalk operator edit --text "See you at one"
walkietalk operator approve     # incoming: release for delivery; outgoing: send now
walkietalk operator transmit    # release one incoming message for delivery right away
walkietalk operator deny        # drop it
walkietalk operator sleep       # close the conversation without speaking the phrase
walkietalk operator status --approved
walkietalk operator transmit --approved
```

The commands find the running instance themselves; pass `-c` if several are
running. Each command is bound to the item and words it showed: if the item
changed before the command ran, it is refused and you review again.
Commands wait until the radio is idle; `--timeout` (default 120 s, up to 600)
limits the wait. Only one operator-mode `talk` runs per config.

- **Read** shows text, or transcribes a voice note (and keeps the transcript).
  Voice must be read before it can be approved or edited.
- **Edit** replaces the words of the item under review. It never approves.
  Empty or unchanged text is not an edit. The radio log shows `Before:` and
  `After:` lines; status shows the received words as `Original:`. Outgoing
  voice notes and voice played as audio cannot be edited; approved items
  cannot be edited.
- **Approve** releases an incoming message for normal delivery: the radio must
  be idle, that contact selected, and (in `conversation` mode) its follow-up
  window open. In `wake-phrase` mode the selected contact's approved messages
  are read whenever the radio is idle. Approving an outgoing message sends it
  to the contact it was captured for, even while asleep.
- **Transmit** releases one reviewed incoming message right away, without a
  wake phrase and without changing the conversation. An earlier message from
  the same contact must be handled first. Sleep or switching conversations
  cancels it, keeping the rest approved; `transmit --approved` resumes it.
- **Deny** drops the item, including any unsent rest.
- **Sleep** closes the conversation, cancels a pending shutdown and any operator
  delivery, keeps the queue and approvals, and speaks the sleep confirmation
  when transmitting.

A failed preparation or transmission returns the item to review; pieces
already transmitted are not repeated, unless the item is then edited (the
edited words are read from the start). Operator mode never skips or retries a
failed item on its own. A failed send keeps the item, since delivery may be
uncertain. A due station ID that fails, or any PTT fault, stops `talk`.

Operator mode decides message approval only; it does not decide whether a
use is permitted by your radio service. The operator must still monitor the
station while it transmits.

### The panel

```bash
walkietalk -c ~/.config/walkietalk/config.toml talk --capture --transmit --panel
```

The panel shows the live radio log above fixed review controls in the same
terminal (at least 60x20). The keys that work at the moment are always shown at
the bottom, as `[R] Read  [E] Edit ...`. Keys: **R** read, **E** edit, **A** approve,
**T** transmit, **D** deny, **S** sleep, **Tab** switch between the review and
approved views, **Up/Down/PgUp/PgDn/Home/End** scroll the message, **Ctrl+C**
stop. The editor accepts typing, **Left/Right**, **Home/End**, **Ctrl+A/E**,
**Backspace/Delete**, **Ctrl+U** (clear), **Enter** (save), and **Esc**
(cancel). Panel actions use the same rules as the commands, which keep working
while the panel is open. While the panel is open, all other program output
goes to its log pane; the terminal is restored on exit.

Control sockets live in `$XDG_RUNTIME_DIR/walkietalk-operator/` (private), or
`~/.cache/walkietalk/operator/` without a runtime directory.
