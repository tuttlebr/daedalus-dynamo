use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use time::OffsetDateTime;
use tokio::sync::broadcast;
use uuid::Uuid;

use crate::models::{
    FileOutcome, JobCompleted, JobEvent, JobFailed, JobFile, JobStarted, JobStatus, JobSummary,
};

const MAX_BROADCAST_CAPACITY: usize = 256;
const COMPLETED_JOB_TTL: Duration = Duration::from_secs(60 * 60);

pub struct JobState {
    pub id: Uuid,
    pub root: String,
    pub recursive: bool,
    pub started_at_rfc: String,
    pub finished_at_rfc: Mutex<Option<String>>,
    pub finished_at_inst: Mutex<Option<std::time::Instant>>,
    pub status: Mutex<JobStatus>,
    pub current_path: Mutex<Option<String>>,
    pub message: Mutex<Option<String>>,
    pub total_seen: Mutex<u64>,
    pub processed: Mutex<u64>,
    pub skipped_dedup: Mutex<u64>,
    pub failed: Mutex<u64>,
    pub transcoded: Mutex<u64>,
    pub bytes_seen: Mutex<u64>,
    pub events: broadcast::Sender<JobEvent>,
}

impl JobState {
    pub fn summary(&self) -> JobSummary {
        JobSummary {
            id: self.id,
            root: self.root.clone(),
            recursive: self.recursive,
            status: *self.status.lock().unwrap(),
            started_at: self.started_at_rfc.clone(),
            finished_at: self.finished_at_rfc.lock().unwrap().clone(),
            total_seen: *self.total_seen.lock().unwrap(),
            processed: *self.processed.lock().unwrap(),
            skipped_dedup: *self.skipped_dedup.lock().unwrap(),
            failed: *self.failed.lock().unwrap(),
            transcoded: *self.transcoded.lock().unwrap(),
            bytes_seen: *self.bytes_seen.lock().unwrap(),
            current_path: self.current_path.lock().unwrap().clone(),
            message: self.message.lock().unwrap().clone(),
        }
    }

    pub fn note_seen(&self) {
        *self.total_seen.lock().unwrap() += 1;
    }

    pub fn note_processed(&self, transcoded: bool, bytes: u64, path: String, duration_ms: u64) {
        *self.processed.lock().unwrap() += 1;
        if transcoded {
            *self.transcoded.lock().unwrap() += 1;
        }
        *self.bytes_seen.lock().unwrap() += bytes;
        *self.current_path.lock().unwrap() = Some(path.clone());
        let _ = self.events.send(JobEvent::File(JobFile {
            path,
            outcome: FileOutcome::Processed,
            duration_ms,
        }));
        let _ = self.events.send(JobEvent::Progress(self.summary()));
    }

    pub fn note_skipped_dedup(&self, path: String) {
        *self.skipped_dedup.lock().unwrap() += 1;
        *self.current_path.lock().unwrap() = Some(path.clone());
        let _ = self.events.send(JobEvent::File(JobFile {
            path,
            outcome: FileOutcome::SkippedDedup,
            duration_ms: 0,
        }));
        let _ = self.events.send(JobEvent::Progress(self.summary()));
    }

    pub fn note_failed(&self, path: String, duration_ms: u64) {
        *self.failed.lock().unwrap() += 1;
        *self.current_path.lock().unwrap() = Some(path.clone());
        let _ = self.events.send(JobEvent::File(JobFile {
            path,
            outcome: FileOutcome::Failed,
            duration_ms,
        }));
        let _ = self.events.send(JobEvent::Progress(self.summary()));
    }

    pub fn finish(&self, status: JobStatus, message: Option<String>) {
        *self.status.lock().unwrap() = status;
        *self.finished_at_rfc.lock().unwrap() = Some(now_rfc3339());
        *self.finished_at_inst.lock().unwrap() = Some(std::time::Instant::now());
        if let Some(m) = &message {
            *self.message.lock().unwrap() = Some(m.clone());
        }
        let summary = self.summary();
        match status {
            JobStatus::Completed => {
                let _ = self.events.send(JobEvent::Completed(JobCompleted {
                    id: self.id,
                    summary,
                }));
            }
            JobStatus::CompletedWithFailures => {
                let _ = self.events.send(JobEvent::Completed(JobCompleted {
                    id: self.id,
                    summary,
                }));
            }
            JobStatus::Failed => {
                let _ = self.events.send(JobEvent::Failed(JobFailed {
                    id: self.id,
                    message: message.unwrap_or_else(|| "job failed".into()),
                }));
            }
            JobStatus::Running => {}
        }
    }

    pub fn note_walk_errors(&self, count: u64) {
        if count == 0 {
            return;
        }
        *self.total_seen.lock().unwrap() += count;
        *self.failed.lock().unwrap() += count;
        *self.message.lock().unwrap() = Some(format!("{count} directory walk errors"));
        let _ = self.events.send(JobEvent::Progress(self.summary()));
    }

    pub fn failed_count(&self) -> u64 {
        *self.failed.lock().unwrap()
    }

    pub fn subscribe(&self) -> broadcast::Receiver<JobEvent> {
        self.events.subscribe()
    }
}

#[derive(Default)]
pub struct JobRegistry {
    inner: Mutex<HashMap<Uuid, Arc<JobState>>>,
}

impl JobRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn create(&self, root: String, recursive: bool) -> Arc<JobState> {
        let id = Uuid::new_v4();
        let (tx, _rx) = broadcast::channel(MAX_BROADCAST_CAPACITY);
        let state = Arc::new(JobState {
            id,
            root: root.clone(),
            recursive,
            started_at_rfc: now_rfc3339(),
            finished_at_rfc: Mutex::new(None),
            finished_at_inst: Mutex::new(None),
            status: Mutex::new(JobStatus::Running),
            current_path: Mutex::new(None),
            message: Mutex::new(None),
            total_seen: Mutex::new(0),
            processed: Mutex::new(0),
            skipped_dedup: Mutex::new(0),
            failed: Mutex::new(0),
            transcoded: Mutex::new(0),
            bytes_seen: Mutex::new(0),
            events: tx,
        });
        let _ = state
            .events
            .send(JobEvent::Started(JobStarted { id, root }));
        self.inner.lock().unwrap().insert(id, Arc::clone(&state));
        state
    }

    pub fn get(&self, id: Uuid) -> Option<Arc<JobState>> {
        self.inner.lock().unwrap().get(&id).cloned()
    }

    pub fn evict_stale(&self) {
        let mut inner = self.inner.lock().unwrap();
        inner.retain(|_, state| {
            let finished = state.finished_at_inst.lock().unwrap();
            match *finished {
                Some(t) => t.elapsed() < COMPLETED_JOB_TTL,
                None => true,
            }
        });
    }
}

fn now_rfc3339() -> String {
    OffsetDateTime::now_utc()
        .format(&time::format_description::well_known::Rfc3339)
        .unwrap_or_default()
}
