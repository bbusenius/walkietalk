//! Answering with the text agent or the realtime voice, and speaking
//! fixed phrases.

use std::time::Instant;

use anyhow::bail;

use super::air::{Air, AirError, Part, Parts};
use super::gap::Gap;
use super::{Brain, Talk};
use crate::radio::BreakIn;
use crate::realtime::Ending;
use crate::{realtime, ui};

/// Why a reply sent in parts stopped before its end.
enum Stop {
    Limit,
    BrokenIn,
    Speech(anyhow::Error),
}

/// Transmit a reply part by part, synthesizing the next part while one is on
/// the air and pausing at `gap` between them. Returns the text that aired,
/// why the reply stopped early, and the failure that ended it.
async fn send_parts(
    air: &mut Air,
    parts: &mut Parts,
    first: Part,
    max: usize,
    gap: &mut Gap<'_>,
) -> (Vec<String>, Option<Stop>, Option<AirError>) {
    let mut aired = Vec::new();
    let mut part = first;
    loop {
        let last_allowed = aired.len() + 1 >= max;
        let (sent, next) = if last_allowed {
            (air.send_part(part.clip).await, Ok(None))
        } else {
            tokio::join!(air.send_part(part.clip), parts.next())
        };
        if let Err(err) = sent {
            return (aired, None, Some(err));
        }
        aired.push(part.text);
        part = match next {
            Ok(Some(next)) => next,
            Ok(None) if last_allowed && !parts.done() => return (aired, Some(Stop::Limit), None),
            Ok(None) => return (aired, None, None),
            Err(err) => return (aired, Some(Stop::Speech(err)), None),
        };
        if gap.listen().await {
            return (aired, Some(Stop::BrokenIn), None);
        }
    }
}

impl Talk {
    /// Ask the text agent and deliver its reply.
    pub(super) async fn answer(&mut self, text: &str) -> anyhow::Result<()> {
        let Talk {
            brain,
            air,
            input,
            carried,
            config,
            gate,
            once,
            ..
        } = self;
        let once = *once;
        let Brain::Text { conversation, .. } = brain else {
            unreachable!("text agent")
        };
        ui::meter!("Asking the agent...");
        let mut reply = match conversation.ask(text).await {
            Ok(reply) => reply,
            Err(err) if once => return Err(err.context("agent failed")),
            Err(err) => {
                ui::error!("Agent failed: {err:#}");
                ui::status!("Still listening; say the wake phrase and try again.");
                return Ok(());
            }
        };
        let Some(air) = air.as_mut() else {
            ui::reply!("Reply: {}", reply.text);
            conversation.commit(reply);
            gate.complete_turn(Instant::now());
            return Ok(());
        };
        ui::meter!("Synthesizing the reply; transmitter off...");
        let first = match air.parts(&reply.text) {
            Ok(mut parts) => match parts.next().await {
                Ok(Some(first)) => Ok((parts, first)),
                Ok(None) => Err(anyhow::anyhow!("nothing to say")),
                Err(err) => Err(err),
            },
            Err(err) => Err(err),
        };
        let (mut parts, first) = match first {
            Ok(first) => first,
            Err(err) if once => return Err(err.context("speech failed")),
            Err(err) => {
                ui::error!("Speech failed: {err:#}");
                ui::status!("The reply was not spoken and is not kept as context.");
                return Ok(());
            }
        };
        ui::reply!("Reply: {}", reply.text);
        let (aired, stop, failure) = if parts.done() {
            (Vec::new(), None, air.send(first.clip).await.err())
        } else {
            let max = config.radio.max_reply_transmissions;
            let mut gap = Gap {
                input,
                carried,
                config,
            };
            send_parts(air, &mut parts, first, max, &mut gap).await
        };
        if !aired.is_empty() {
            match &stop {
                None => {}
                Some(Stop::Limit) => ui::warning!(
                    "The reply used all {} transmissions it may; the rest was not sent.",
                    config.radio.max_reply_transmissions
                ),
                Some(Stop::BrokenIn) => {
                    ui::warning!("Someone started talking; the rest of the reply was not sent.")
                }
                Some(Stop::Speech(err)) => {
                    ui::error!("Speech failed for the rest of the reply: {err:#}")
                }
            }
            if stop.is_some() || failure.is_some() {
                reply.text = format!(
                    "{} [The radio stopped here; the listener did not hear the rest of this reply.]",
                    aired.join(" ")
                );
            }
            conversation.commit(reply);
            gate.complete_turn(Instant::now());
            return match failure {
                // Nothing more goes out over a station that broke in.
                None if matches!(stop, Some(Stop::BrokenIn)) => Ok(()),
                None => Ok(air.finish_reply().await?),
                Some(AirError::Fatal { message, .. }) => bail!(message),
                Some(err) if once => {
                    Err(anyhow::Error::from(err)
                        .context("the rest of the reply was not transmitted"))
                }
                Some(err) => {
                    ui::error!("The rest of the reply was not transmitted: {err:#}");
                    Ok(())
                }
            };
        }
        match failure {
            None => {
                conversation.commit(reply);
                gate.complete_turn(Instant::now());
                Ok(())
            }
            Some(AirError::NotSent(err)) if !once => {
                ui::error!("The reply was not transmitted: {err:#}");
                Ok(())
            }
            Some(AirError::NotSent(err)) => Err(err.context("the reply was not transmitted")),
            Some(AirError::Playback(err)) if !once => {
                ui::error!("Playback failed while transmitting: {err:#}");
                ui::status!("The reply may have been partly heard; it is not kept as context.");
                Ok(())
            }
            Some(AirError::Playback(err)) => {
                Err(err.context("playback failed while transmitting; the transmitter was released"))
            }
            Some(AirError::Fatal {
                message,
                reply_sent,
            }) => {
                if reply_sent {
                    conversation.commit(reply);
                }
                bail!(message)
            }
        }
    }

    /// Ask the realtime voice to answer the committed audio.
    pub(super) async fn answer_realtime(&mut self) -> anyhow::Result<()> {
        let Talk {
            brain,
            air,
            input,
            carried,
            config,
            once,
            ..
        } = self;
        let Brain::Realtime(session) = brain else {
            unreachable!("realtime")
        };
        let radio = air.as_ref().map(|air| air.radio().clone());
        ui::meter!("Voice turn; the transmitter keys only when speech arrives...");
        let mut gap = Gap {
            input,
            carried,
            config,
        };
        let reply = match session.respond(radio, Some(&mut gap)).await {
            Ok(reply) => reply,
            Err(err) if realtime::is_ptt_fault(&err) || realtime::is_auth_error(&err) || *once => {
                return Err(err);
            }
            Err(err) => {
                ui::error!("Voice agent failed: {err:#}");
                ui::status!("Still listening; say the wake phrase and try again.");
                return Ok(());
            }
        };
        if !reply.heard.is_empty() {
            ui::transcript!("Heard: {}", reply.heard);
        }
        if !reply.audible {
            if self.once {
                bail!("the voice agent returned no audible reply");
            }
            ui::warning!(
                "The voice agent returned no audible reply; the follow-up window stays closed."
            );
            return Ok(());
        }
        ui::reply!(
            "Reply: {}",
            if reply.said.is_empty() {
                "(audio only)"
            } else {
                &reply.said
            }
        );
        match reply.ending {
            Ending::Complete => {}
            Ending::Limit => ui::warning!(
                "The reply used all {} transmissions it may; the rest was not sent.",
                self.config.radio.max_reply_transmissions
            ),
            Ending::BrokenIn => {
                ui::warning!("Someone started talking; the rest of the reply was not sent.")
            }
        }
        if reply.truncated {
            ui::warning!(
                "The transmit cap cut the reply short; the next turn starts a fresh conversation."
            );
        }
        // Nothing more goes out over a station that broke in.
        if self.air.is_some() && reply.ending != Ending::BrokenIn {
            self.realtime_station_id().await?;
            if let Some(air) = &self.air {
                air.mute().await;
            }
        }
        if !reply.truncated {
            self.gate.complete_turn(Instant::now());
        }
        Ok(())
    }

    /// A due station ID after a realtime reply, in its own transmission.
    async fn realtime_station_id(&mut self) -> anyhow::Result<()> {
        let (Some(air), Brain::Realtime(session)) = (self.air.as_mut(), &self.brain) else {
            return Ok(());
        };
        if !air.station_id().due(Instant::now()) {
            return Ok(());
        }
        ui::status!("Station ID follows in its own transmission.");
        tokio::time::sleep(crate::radio::station_id::GAP).await;
        let sent = match air.station_id().config().method {
            crate::config::StationIdMethod::Morse => {
                let clip = crate::audio::morse::morse(air.station_id().callsign())?;
                air.transmit(clip).await.map_err(anyhow::Error::from)
            }
            crate::config::StationIdMethod::Voice => {
                let callsign = air.station_id().callsign().to_string();
                match realtime::speak(session.settings(), &callsign, Some(air.radio().clone()))
                    .await
                {
                    Ok(reply) if reply.complete() => Ok(()),
                    Ok(_) => Err(anyhow::anyhow!(
                        "the station ID was not transmitted in full"
                    )),
                    Err(err) => Err(err),
                }
            }
        };
        match sent {
            Ok(()) => {
                air.station_id_mut().mark_sent(Instant::now());
                Ok(())
            }
            Err(err) => bail!("station ID failed: {err:#}; stopping"),
        }
    }

    /// Speak a fixed phrase when transmitting; receive-only stays silent.
    pub(super) async fn say(&mut self, text: &str, what: &str) -> anyhow::Result<bool> {
        if text.is_empty() {
            return Ok(false);
        }
        let Some(air) = self.air.as_mut() else {
            ui::meter!("{what} not spoken (receive only).");
            return Ok(false);
        };
        if let Brain::Realtime(session) = &self.brain {
            let result = realtime::speak(session.settings(), text, Some(air.radio().clone())).await;
            return match result {
                Ok(reply) if reply.complete() => {
                    // As with the text path, a confirmation carries an ID only
                    // when an interval ID is due.
                    let interval =
                        air.station_id().config().mode == crate::config::StationIdMode::Interval;
                    if interval {
                        self.realtime_station_id().await?;
                    }
                    if let Some(air) = &self.air {
                        air.mute().await;
                    }
                    Ok(true)
                }
                Ok(_) => {
                    ui::error!("{what} was not transmitted in full.");
                    Ok(false)
                }
                Err(err) if realtime::is_ptt_fault(&err) => Err(err),
                Err(err) => {
                    ui::error!("{what} failed: {err:#}");
                    Ok(false)
                }
            };
        }
        air.say(text, what)
            .await
            .map_err(|err| anyhow::anyhow!("{err}"))
    }
}
