//! End-to-end tests of the talk loop with fake audio, recognizer, agent,
//! voice, transmitter, and messaging contacts.

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use tokio::sync::mpsc;

use super::*;
use crate::agent::tests::Scripted;
use crate::audio::Fit;
use crate::config::tests_support::MINIMAL;
use crate::messaging::{Contact, Content, Inbound};
use crate::operator::{Action, Response, Shared};
use crate::radio::ptt::fake::FakeLine;
use crate::radio::tests::{FakeOut, timing};

const RATE: u32 = 16_000;

/// Returns scripted transcripts, one per utterance.
struct ScriptedStt(Mutex<VecDeque<&'static str>>);

#[async_trait]
impl Transcriber for ScriptedStt {
    fn label(&self) -> String {
        "scripted".into()
    }
    async fn prepare(&self) -> anyhow::Result<()> {
        Ok(())
    }
    async fn transcribe(&self, _audio: &crate::audio::Clip) -> anyhow::Result<String> {
        Ok(self.0.lock().unwrap().pop_front().unwrap_or("").to_string())
    }
}

/// Speaks at ten characters per second and records what it said.
#[derive(Default)]
struct FakeVoice {
    said: Mutex<Vec<String>>,
}

#[async_trait]
impl crate::tts::Voice for FakeVoice {
    fn label(&self) -> String {
        "fake".into()
    }
    async fn prepare(&self) -> anyhow::Result<()> {
        Ok(())
    }
    async fn synthesize(&self, text: &str, fit: Fit) -> anyhow::Result<crate::audio::Clip> {
        self.said.lock().unwrap().push(text.to_string());
        let clip = crate::audio::Clip::silence(
            Duration::from_millis(text.chars().count() as u64 * 100),
            48_000,
        );
        Ok(clip.fit(Duration::from_millis(9_800), fit)?)
    }
}

/// Records what was sent to a contact.
#[derive(Clone, Default)]
struct FakeContact {
    sent: Arc<Mutex<Vec<String>>>,
}

#[async_trait]
impl Contact for FakeContact {
    async fn send_text(&self, text: &str) -> anyhow::Result<()> {
        self.sent.lock().unwrap().push(text.to_string());
        Ok(())
    }
    async fn send_voice(&self, _file: &std::path::Path) -> anyhow::Result<()> {
        self.sent.lock().unwrap().push("<voice>".into());
        Ok(())
    }
    async fn stop(self: Box<Self>) {}
}

struct Rig {
    talk: Talk,
    frames: mpsc::Sender<Vec<i16>>,
    line: FakeLine,
    voice: Arc<FakeVoice>,
    agent: Scripted,
    contact: FakeContact,
    inbox: Option<mpsc::Sender<Inbound>>,
    operator: Option<(mpsc::Sender<Command>, Shared)>,
}

fn rig(extra: &str, transcripts: Vec<&'static str>, out: FakeOut) -> Rig {
    // No post-transmit pause, so tests run quickly.
    let config = Config::parse(
        &format!("{MINIMAL}\n[radio]\npost_tx_mute_seconds = 0\n{extra}"),
        "/".into(),
    )
    .unwrap();
    let line = FakeLine::default();
    let voice = Arc::new(FakeVoice::default());
    let radio = Radio::with_parts(Box::new(line.clone()), Box::new(out), timing(10_000));
    let air = Air::new(radio, Some(voice.clone()), &config);
    let agent = Scripted::default();
    let stt: Arc<dyn Transcriber> = Arc::new(ScriptedStt(Mutex::new(transcripts.into())));
    let (frames, rx) = mpsc::channel(100_000);
    let contact = FakeContact::default();
    let (mut inbox, mut operator, mut messaging) = (None, None, None);
    if config.messaging_enabled() {
        let contacts: HashMap<Service, Box<dyn Contact>> = config
            .messaging
            .enabled()
            .map(|(s, _)| (s, Box::new(contact.clone()) as Box<dyn Contact>))
            .collect();
        let (tx, inbox_rx) = mpsc::channel(16);
        inbox = Some(tx);
        let op = config.messaging.operator_mode.then(|| {
            let (op, commands, shared) = operator::Operator::for_tests(&config);
            operator = Some((commands, shared));
            op
        });
        messaging = Some(Messaging {
            bridge: Bridge::with(contacts),
            inbox: inbox_rx,
            queues: Queues::default(),
            progress: HashMap::new(),
            next_at: Instant::now(),
            notes: Some(stt.clone()),
            operator: op,
        });
    }
    let talk = Talk {
        gate: Gate::new(&config),
        shutdown: Shutdown::new(&config),
        brain: Brain::Text {
            stt,
            conversation: Conversation::new(Box::new(agent.clone()), &config, true),
        },
        config,
        creds: Credentials::environment_only(),
        air: Some(air),
        messaging,
        input: Input::Frames(Some(rx)),
        once: false,
        wait: None,
        stop: CancellationToken::new(),
    };
    Rig {
        talk,
        frames,
        line,
        voice,
        agent,
        contact,
        inbox,
        operator,
    }
}

/// Queue one utterance: half a second of speech, then enough silence to end it.
async fn speak(frames: &mpsc::Sender<Vec<i16>>) {
    let frame = |level: i16| {
        (0..RATE as usize / 50)
            .map(|i| if i % 2 == 0 { level } else { -level })
            .collect::<Vec<_>>()
    };
    for _ in 0..25 {
        frames.send(frame(8_000)).await.unwrap();
    }
    for _ in 0..30 {
        frames.send(frame(0)).await.unwrap();
    }
}

/// Run the loop until input ends (or a test time limit).
async fn run(rig: &mut Rig) -> anyhow::Result<()> {
    tokio::time::timeout(Duration::from_secs(20), rig.talk.run())
        .await
        .expect("talk loop hung")
}

#[tokio::test]
async fn addressed_question_is_answered_on_air_and_remembered() {
    let mut r = rig(
        "",
        vec!["Charlotte, why is the sky blue?", "and at sunset?"],
        FakeOut::default(),
    );
    speak(&r.frames).await;
    speak(&r.frames).await;
    r.frames = mpsc::channel(1).0;
    run(&mut r).await.unwrap();
    let seen = r.agent.seen.lock().unwrap().clone();
    assert_eq!(seen.len(), 2, "the follow-up within the window is accepted");
    assert_eq!(seen[0].1, "why is the sky blue?");
    assert_eq!(
        seen[1].0.len(),
        1,
        "the delivered first turn is history for the second"
    );
    assert!(r.line.changes().contains(&true));
    assert!(!r.line.keyed(), "the transmitter is released afterwards");
}

#[tokio::test]
async fn unaddressed_speech_is_ignored_and_never_transmitted() {
    let mut r = rig("", vec!["what time is it"], FakeOut::default());
    speak(&r.frames).await;
    r.frames = mpsc::channel(1).0;
    run(&mut r).await.unwrap();
    assert!(r.agent.seen.lock().unwrap().is_empty());
    assert!(r.line.changes().is_empty());
}

#[tokio::test]
async fn unsent_reply_is_not_kept_as_history() {
    let out = FakeOut {
        fail_prepare: true,
        ..Default::default()
    };
    let mut r = rig("", vec!["charlotte first", "charlotte second"], out);
    speak(&r.frames).await;
    speak(&r.frames).await;
    r.frames = mpsc::channel(1).0;
    run(&mut r).await.unwrap();
    let seen = r.agent.seen.lock().unwrap().clone();
    assert_eq!(
        seen.len(),
        2,
        "listening continues after a failed transmission"
    );
    assert!(seen[1].0.is_empty(), "the unheard reply is not context");
    assert!(!r.line.changes().contains(&true), "nothing was keyed");
}

#[tokio::test]
async fn ptt_fault_stops_talk_with_the_line_released() {
    let mut r = rig("", vec!["charlotte hello"], FakeOut::default());
    *r.line.fail_key.lock().unwrap() = true;
    speak(&r.frames).await;
    let err = run(&mut r).await.unwrap_err();
    assert!(err.to_string().contains("PTT"), "{err}");
    assert!(!r.line.keyed());
}

#[tokio::test]
async fn shutdown_phrase_and_code_stop_after_confirming() {
    let shutdown = "[shutdown]\nenabled = true\nphrase = \"bird\"\ncode = \"seven\"\narmed_reply = \"Armed.\"\nconfirmed_reply = \"Goodbye.\"\n";
    let mut r = rig(
        shutdown,
        vec!["bird", "seven", "charlotte never asked"],
        FakeOut::default(),
    );
    for _ in 0..3 {
        speak(&r.frames).await;
    }
    run(&mut r).await.unwrap();
    assert_eq!(
        r.voice.said.lock().unwrap().as_slice(),
        ["Armed.", "Goodbye."]
    );
    assert!(
        r.agent.seen.lock().unwrap().is_empty(),
        "controls never reach the agent"
    );
}

#[tokio::test]
async fn sleep_closes_the_follow_up_window() {
    let sleep = "[sleep]\nphrase = \"go to sleep\"\nconfirmation = \"Standing by.\"\n";
    let mut r = rig(
        sleep,
        vec!["charlotte hi", "go to sleep", "are you there"],
        FakeOut::default(),
    );
    for _ in 0..3 {
        speak(&r.frames).await;
    }
    r.frames = mpsc::channel(1).0;
    run(&mut r).await.unwrap();
    assert_eq!(r.agent.seen.lock().unwrap().len(), 1);
    assert!(
        r.voice
            .said
            .lock()
            .unwrap()
            .contains(&"Standing by.".to_string())
    );
}

#[tokio::test]
async fn station_id_follows_the_reply_and_is_sent_once_per_interval() {
    let id =
        "[radio.station_id]\ncallsign = \"TEST123\"\nmode = \"interval\"\nmethod = \"voice\"\n";
    let mut r = rig(id, vec!["charlotte one", "two"], FakeOut::default());
    speak(&r.frames).await;
    speak(&r.frames).await;
    r.frames = mpsc::channel(1).0;
    run(&mut r).await.unwrap();
    let ids = r
        .voice
        .said
        .lock()
        .unwrap()
        .iter()
        .filter(|s| *s == "TEST123")
        .count();
    assert_eq!(ids, 1, "the interval has not passed for the second reply");
}

const CONTACT: &str = "[messaging.signal]\nwake = \"grandma\"\nto = \"+15557654321\"\nsender_alias = \"Nana\"\nempty_queue_reply = \"No new messages.\"\n";

#[tokio::test]
async fn contact_traffic_is_sent_and_empty_queue_is_announced() {
    let mut r = rig(
        CONTACT,
        vec!["grandma", "grandma see you soon"],
        FakeOut::default(),
    );
    speak(&r.frames).await;
    speak(&r.frames).await;
    r.frames = mpsc::channel(1).0;
    run(&mut r).await.unwrap();
    assert!(
        r.voice
            .said
            .lock()
            .unwrap()
            .contains(&"No new messages.".to_string())
    );
    assert_eq!(r.contact.sent.lock().unwrap().as_slice(), ["see you soon"]);
}

#[tokio::test]
async fn incoming_message_is_read_with_its_sender_when_the_contact_is_open() {
    let mut r = rig(CONTACT, vec!["grandma"], FakeOut::default());
    r.inbox
        .as_ref()
        .unwrap()
        .send(Inbound {
            service: Service::Signal,
            id: "1".into(),
            content: Content::Text("Dinner at six!".into()),
        })
        .await
        .unwrap();
    speak(&r.frames).await;
    r.frames = mpsc::channel(1).0;
    run(&mut r).await.unwrap();
    let said = r.voice.said.lock().unwrap().clone();
    assert!(
        said.contains(&"Nana says: Dinner at six, over".to_string()),
        "{said:?}"
    );
    assert!(
        r.talk
            .messaging
            .as_ref()
            .unwrap()
            .queues
            .head(Service::Signal)
            .is_none(),
        "delivered messages leave the queue"
    );
}

async fn command(
    tx: &mpsc::Sender<Command>,
    shared: &Shared,
    action: Action,
    approved: bool,
    text: Option<&str>,
) -> mpsc::UnboundedReceiver<Response> {
    let revision = crate::operator::lock(shared).review.revision(approved);
    let (cmd, replies, _) = Command::new(action, approved, revision, text.map(String::from));
    tx.send(cmd).await.unwrap();
    replies
}

/// Run the next queued operator command, as the loop does between radio activity.
async fn run_pending(talk: &mut Talk) {
    let command = talk.pending_command().expect("a queued command");
    talk.operator_command(command).await.unwrap();
}

fn finished_ok(replies: &mut mpsc::UnboundedReceiver<Response>) -> bool {
    let mut ok = false;
    while let Ok(reply) = replies.try_recv() {
        if let Response::Done { ok: done, .. } = reply {
            ok = done;
        }
    }
    ok
}

#[tokio::test]
async fn operator_mode_holds_messages_until_approved() {
    let extra = format!("[messaging]\noperator_mode = true\n{CONTACT}");
    let mut r = rig(&extra, vec!["grandma hello there"], FakeOut::default());
    let (tx, shared) = r.operator.clone().unwrap();
    // Outgoing traffic waits for review instead of being sent.
    speak(&r.frames).await;
    r.frames = mpsc::channel(1).0;
    run(&mut r).await.unwrap();
    assert!(r.contact.sent.lock().unwrap().is_empty());
    // An incoming message also waits.
    r.talk.accept(Inbound {
        service: Service::Signal,
        id: "in1".into(),
        content: Content::Text("Call me".into()),
    });
    let mut approve = command(&tx, &shared, Action::Approve, false, None).await;
    run_pending(&mut r.talk).await;
    assert!(finished_ok(&mut approve));
    assert_eq!(
        r.contact.sent.lock().unwrap().as_slice(),
        ["hello there"],
        "approval sends the outgoing message"
    );
    r.talk.gate.decide("grandma", Instant::now());
    r.talk.gate.complete_turn(Instant::now());
    assert_eq!(
        r.talk.ready_service(),
        None,
        "an unapproved incoming message is never delivered"
    );
}

#[tokio::test]
async fn operator_transmit_delivers_one_edited_message_without_a_wake() {
    let extra = format!("[messaging]\noperator_mode = true\n{CONTACT}");
    let mut r = rig(&extra, vec![], FakeOut::default());
    let (tx, shared) = r.operator.clone().unwrap();
    r.talk.accept(Inbound {
        service: Service::Signal,
        id: "in1".into(),
        content: Content::Text("See you at noon".into()),
    });
    let mut edit = command(&tx, &shared, Action::Edit, false, Some("See you at one")).await;
    // Commands run in order between radio activity.
    run_pending(&mut r.talk).await;
    assert!(finished_ok(&mut edit));
    let mut transmit = command(&tx, &shared, Action::Transmit, false, None).await;
    run_pending(&mut r.talk).await;
    assert!(finished_ok(&mut transmit));
    assert_eq!(
        r.talk.ready_service(),
        Some(Service::Signal),
        "operator delivery needs no wake"
    );
    assert!(r.talk.deliver(Service::Signal).await.unwrap());
    let said = r.voice.said.lock().unwrap().clone();
    assert_eq!(said, ["Nana says: See you at one, over"]);
    assert_eq!(
        r.talk.gate.selected(),
        None,
        "the conversation is unchanged"
    );
}

#[tokio::test]
async fn stale_operator_commands_are_refused() {
    let extra = format!("[messaging]\noperator_mode = true\n{CONTACT}");
    let mut r = rig(&extra, vec![], FakeOut::default());
    let (tx, shared) = r.operator.clone().unwrap();
    r.talk.accept(Inbound {
        service: Service::Signal,
        id: "a".into(),
        content: Content::Text("First".into()),
    });
    let mut approve = command(&tx, &shared, Action::Approve, false, None).await;
    // The displayed item changes before the command runs.
    crate::operator::lock(&shared).review.edit(1, "Changed");
    run_pending(&mut r.talk).await;
    assert!(!finished_ok(&mut approve));
    assert!(crate::operator::lock(&shared).review.is_waiting(1));
}

#[tokio::test]
async fn playback_failures_are_survived_until_they_repeat() {
    let out = FakeOut {
        fail_playback: true,
        ..Default::default()
    };
    let mut r = rig(
        "",
        vec!["charlotte one", "charlotte two", "charlotte three"],
        out,
    );
    for _ in 0..3 {
        speak(&r.frames).await;
    }
    let err = run(&mut r).await.unwrap_err();
    assert!(err.to_string().contains("3 times"), "{err}");
    let seen = r.agent.seen.lock().unwrap().clone();
    assert_eq!(
        seen.len(),
        3,
        "listening continued after the first failures"
    );
    assert!(
        seen.iter().all(|(history, _)| history.is_empty()),
        "failed replies are never context"
    );
    assert!(!r.line.keyed());
}
