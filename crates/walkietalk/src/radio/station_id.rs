//! Station identification: when it is due, and how it shares airtime.

use std::time::{Duration, Instant};

use crate::audio::{Clip, Fit, TooLong};
use crate::config::{StationIdConfig, StationIdMode};

/// Silence between a reply and an appended ID, and the unkeyed pause
/// between two separate bursts.
pub const GAP: Duration = Duration::from_millis(200);

/// Tracks when the ID was last sent successfully.
#[derive(Debug, Clone)]
pub struct StationId {
    config: StationIdConfig,
    last_sent: Option<Instant>,
}

impl StationId {
    pub fn new(config: StationIdConfig) -> StationId {
        StationId {
            config,
            last_sent: None,
        }
    }

    pub fn callsign(&self) -> &str {
        &self.config.callsign
    }

    pub fn config(&self) -> &StationIdConfig {
        &self.config
    }

    pub fn due(&self, now: Instant) -> bool {
        if !self.config.enabled() {
            return false;
        }
        match (self.config.mode, self.last_sent) {
            (StationIdMode::Off, _) => false,
            (StationIdMode::EndOfReply, _) | (StationIdMode::Interval, None) => true,
            (StationIdMode::Interval, Some(last)) => {
                now.duration_since(last) >= Duration::from_secs_f64(self.config.interval_seconds)
            }
        }
    }

    /// Record a completed ID. Call only after it was transmitted in full.
    pub fn mark_sent(&mut self, at: Instant) {
        self.last_sent = Some(at);
    }
}

/// The transmissions for a reply with an ID: one burst when the whole reply,
/// gap, and ID fit; otherwise the reply, then the ID on its own.
/// The ID is never cropped.
pub fn plan(reply: Clip, id: Clip, budget: Duration) -> Result<Vec<Clip>, TooLong> {
    let reply = reply.to_radio();
    let id = id.to_radio().fit(budget, Fit::Strict)?;
    let mut combined = reply.clone();
    combined.append(&Clip::silence(GAP, combined.rate()));
    combined.append(&id);
    match combined.fit(budget, Fit::Strict) {
        Ok(one) => Ok(vec![one]),
        Err(_) => Ok(vec![reply, id]),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config(mode: StationIdMode) -> StationIdConfig {
        StationIdConfig {
            callsign: "TEST123".into(),
            mode,
            interval_seconds: 60.0,
            ..Default::default()
        }
    }

    fn secs(s: f64) -> Clip {
        Clip::silence(Duration::from_secs_f64(s), 48_000)
    }

    #[test]
    fn interval_is_due_first_and_after_the_interval() {
        let mut id = StationId::new(config(StationIdMode::Interval));
        let t0 = Instant::now();
        assert!(id.due(t0));
        id.mark_sent(t0);
        assert!(!id.due(t0 + Duration::from_secs(59)));
        assert!(id.due(t0 + Duration::from_secs(60)));
    }

    #[test]
    fn end_of_reply_is_always_due_and_off_never() {
        let mut id = StationId::new(config(StationIdMode::EndOfReply));
        id.mark_sent(Instant::now());
        assert!(id.due(Instant::now()));
        assert!(!StationId::new(config(StationIdMode::Off)).due(Instant::now()));
        let empty = StationIdConfig {
            callsign: String::new(),
            ..config(StationIdMode::EndOfReply)
        };
        assert!(!StationId::new(empty).due(Instant::now()));
    }

    #[test]
    fn id_is_appended_when_it_fits() {
        let bursts = plan(secs(5.0), secs(2.0), Duration::from_secs(10)).unwrap();
        assert_eq!(bursts.len(), 1);
        assert!((bursts[0].seconds() - 7.2).abs() < 0.001);
    }

    #[test]
    fn id_gets_its_own_burst_when_it_does_not_fit() {
        let bursts = plan(secs(9.0), secs(2.0), Duration::from_secs(10)).unwrap();
        assert_eq!(bursts.len(), 2);
        assert!(
            (bursts[1].seconds() - 2.0).abs() < 0.001,
            "the ID is never cropped"
        );
    }

    #[test]
    fn an_id_longer_than_a_transmission_is_refused() {
        assert!(plan(secs(1.0), secs(11.0), Duration::from_secs(10)).is_err());
    }
}
