//! Answering with the text agent or the realtime voice, and speaking
//! fixed phrases.

use std::time::Instant;

use anyhow::bail;

use super::air::AirError;
use super::{Brain, Talk};
use crate::{realtime, ui};

impl Talk {
    /// Ask the text agent and deliver its reply.
    pub(super) async fn answer(&mut self, text: &str) -> anyhow::Result<()> {
        let Brain::Text { conversation, .. } = &mut self.brain else {
            unreachable!("text agent")
        };
        ui::meter!("Asking the agent...");
        let reply = match conversation.ask(text).await {
            Ok(reply) => reply,
            Err(err) if self.once => return Err(err.context("agent failed")),
            Err(err) => {
                ui::error!("Agent failed: {err:#}");
                ui::status!("Still listening; say the wake phrase and try again.");
                return Ok(());
            }
        };
        let Some(air) = self.air.as_mut() else {
            ui::reply!("Reply: {}", reply.text);
            conversation.commit(reply);
            self.gate.complete_turn(Instant::now());
            return Ok(());
        };
        ui::meter!("Synthesizing the reply; transmitter off...");
        let clip = match air.synthesize(&reply.text).await {
            Ok(clip) => clip,
            Err(err) if self.once => return Err(err.context("speech failed")),
            Err(err) => {
                ui::error!("Speech failed: {err:#}");
                ui::status!("The reply was not spoken and is not kept as context.");
                return Ok(());
            }
        };
        ui::reply!("Reply: {}", reply.text);
        match air.send(clip).await {
            Ok(()) => {
                conversation.commit(reply);
                self.gate.complete_turn(Instant::now());
                Ok(())
            }
            Err(AirError::NotSent(err)) if !self.once => {
                ui::error!("The reply was not transmitted: {err:#}");
                Ok(())
            }
            Err(AirError::NotSent(err)) => Err(err.context("the reply was not transmitted")),
            Err(AirError::Playback(err)) if !self.once => {
                ui::error!("Playback failed while transmitting: {err:#}");
                ui::status!("The reply may have been partly heard; it is not kept as context.");
                Ok(())
            }
            Err(AirError::Playback(err)) => {
                Err(err.context("playback failed while transmitting; the transmitter was released"))
            }
            Err(AirError::Fatal {
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
        let Brain::Realtime(session) = &mut self.brain else {
            unreachable!("realtime")
        };
        let radio = self.air.as_ref().map(|air| air.radio().clone());
        ui::meter!("Voice turn; the transmitter keys only when speech arrives...");
        let reply = match session.respond(radio).await {
            Ok(reply) => reply,
            Err(err)
                if realtime::is_ptt_fault(&err) || realtime::is_auth_error(&err) || self.once =>
            {
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
        if reply.truncated {
            ui::warning!(
                "The transmit cap cut the reply short; the next turn starts a fresh conversation."
            );
        }
        if self.air.is_some() {
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
                    Ok(reply) if reply.audible && !reply.truncated => Ok(()),
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
                Ok(reply) if reply.audible && !reply.truncated => {
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
