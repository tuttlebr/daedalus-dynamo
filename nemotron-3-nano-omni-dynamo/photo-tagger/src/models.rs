use serde::{Deserialize, Serialize};
use uuid::Uuid;

pub const SCHEMA_VERSION: &str = "1.0.0";
pub const PROMPT_VERSION: &str = "2026-05-02.a";

// =============================================================================
// Persisted metadata
// =============================================================================

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct PhotoMetadata {
    pub schema_version: String,
    pub source_path: String,
    pub file_sha256: String,
    pub file_size_bytes: u64,
    pub file_mtime_rfc3339: String,
    pub original_mime_type: String,
    pub width: u32,
    pub height: u32,
    pub processing_model: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub model_version: Option<String>,
    pub prompt_version: String,
    pub processed_at_rfc3339: String,
    pub processing_duration_ms: u64,
    pub raw_response: String,
    pub content: PhotoContent,
}

#[derive(Debug, Serialize, Deserialize, Clone, Default)]
pub struct PhotoContent {
    pub caption: String,
    pub description: String,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default)]
    pub scene: String,
    #[serde(default)]
    pub setting_type: String,
    #[serde(default)]
    pub detected_text: Vec<String>,
    #[serde(default)]
    pub dominant_colors: Vec<String>,
    #[serde(default)]
    pub people: Vec<PersonObservation>,
    #[serde(default)]
    pub objects: Vec<String>,
    #[serde(default)]
    pub quality: QualityAssessment,
}

impl PhotoContent {
    pub fn validate(&self) -> Result<(), String> {
        validate_allowed(
            "setting_type",
            &self.setting_type,
            &[
                "indoor",
                "outdoor",
                "studio",
                "underwater",
                "aerial",
                "mixed",
                "unknown",
            ],
        )?;
        for person in &self.people {
            if let Some(age_range) = &person.apparent_age_range {
                validate_allowed(
                    "people.apparent_age_range",
                    age_range,
                    &["child", "teen", "adult", "senior", "mixed"],
                )?;
            }
        }
        validate_allowed(
            "quality.sharpness",
            &self.quality.sharpness,
            &["sharp", "slightly_soft", "blurry"],
        )?;
        validate_allowed(
            "quality.exposure",
            &self.quality.exposure,
            &["balanced", "underexposed", "overexposed"],
        )?;
        if !(1..=10).contains(&self.quality.overall_score) {
            return Err(format!(
                "quality.overall_score must be between 1 and 10, got {}",
                self.quality.overall_score
            ));
        }
        Ok(())
    }
}

fn validate_allowed(field: &str, value: &str, allowed: &[&str]) -> Result<(), String> {
    if allowed.contains(&value) {
        Ok(())
    } else {
        Err(format!("{field} has unsupported value {value:?}"))
    }
}

#[derive(Debug, Serialize, Deserialize, Clone, Default)]
pub struct PersonObservation {
    pub count_estimate: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub apparent_age_range: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub activity: Option<String>,
}

#[derive(Debug, Serialize, Deserialize, Clone, Default)]
pub struct QualityAssessment {
    pub sharpness: String,
    pub exposure: String,
    pub composition: String,
    pub overall_score: u8,
}

// =============================================================================
// API request / response types
// =============================================================================

#[derive(Debug, Deserialize)]
pub struct ScanRequest {
    pub path: String,
    #[serde(default = "default_recursive")]
    pub recursive: bool,
}
fn default_recursive() -> bool {
    true
}

#[derive(Debug, Serialize)]
pub struct ScanResponse {
    pub job_id: Uuid,
}

#[derive(Debug, Deserialize)]
pub struct ReprocessRequest {
    pub path: String,
}

#[derive(Debug, Serialize)]
pub struct ReprocessResponse {
    pub job_id: Uuid,
}

#[derive(Debug, Serialize)]
pub struct HealthResponse {
    pub status: &'static str,
}

#[derive(Debug, Serialize)]
pub struct ReadyResponse {
    pub status: &'static str,
    pub database: bool,
    pub endpoint_reachable: bool,
}

#[derive(Debug, Serialize)]
pub struct StatsResponse {
    pub total: u64,
    pub processed: u64,
    pub failed: u64,
    pub failed_paths: u64,
    pub paths: u64,
    pub by_setting_type: Vec<CountEntry>,
    pub by_model: Vec<CountEntry>,
}

#[derive(Debug, Serialize)]
pub struct CountEntry {
    pub key: String,
    pub count: u64,
}

#[derive(Debug, Serialize)]
pub struct SearchHit {
    pub sha256: String,
    pub path: String,
    pub caption: String,
    pub tags: Vec<String>,
    pub setting_type: String,
    pub processed_at: String,
}

#[derive(Debug, Serialize)]
pub struct SearchResponse {
    pub query: String,
    pub total: usize,
    pub hits: Vec<SearchHit>,
}

#[derive(Debug, Serialize)]
pub struct BrowseResponse {
    pub total: u64,
    pub hits: Vec<SearchHit>,
}

// =============================================================================
// Job state
// =============================================================================

#[derive(Debug, Serialize, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum JobStatus {
    Running,
    Completed,
    CompletedWithFailures,
    Failed,
}

impl JobStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            JobStatus::Running => "running",
            JobStatus::Completed => "completed",
            JobStatus::CompletedWithFailures => "completed_with_failures",
            JobStatus::Failed => "failed",
        }
    }

    pub fn from_str(value: &str) -> Option<Self> {
        match value {
            "running" => Some(JobStatus::Running),
            "completed" => Some(JobStatus::Completed),
            "completed_with_failures" => Some(JobStatus::CompletedWithFailures),
            "failed" => Some(JobStatus::Failed),
            _ => None,
        }
    }
}

#[derive(Debug, Serialize, Clone)]
pub struct JobSummary {
    pub id: Uuid,
    pub root: String,
    pub recursive: bool,
    pub status: JobStatus,
    pub started_at: String,
    pub finished_at: Option<String>,
    pub total_seen: u64,
    pub processed: u64,
    pub skipped_dedup: u64,
    pub failed: u64,
    pub transcoded: u64,
    pub bytes_seen: u64,
    pub current_path: Option<String>,
    pub message: Option<String>,
}

/// Event broadcast on the per-job channel. Variant data is the on-the-wire JSON
/// payload (see `routes::event_for` for the SSE event name mapping).
#[derive(Debug, Serialize, Clone)]
#[serde(untagged)]
pub enum JobEvent {
    Started(JobStarted),
    File(JobFile),
    Progress(JobSummary),
    Completed(JobCompleted),
    Failed(JobFailed),
}

#[derive(Debug, Serialize, Clone)]
pub struct JobStarted {
    pub id: Uuid,
    pub root: String,
}

#[derive(Debug, Serialize, Clone)]
pub struct JobFile {
    pub path: String,
    pub outcome: FileOutcome,
    pub duration_ms: u64,
}

#[derive(Debug, Serialize, Clone)]
pub struct JobCompleted {
    pub id: Uuid,
    pub summary: JobSummary,
}

#[derive(Debug, Serialize, Clone)]
pub struct JobFailed {
    pub id: Uuid,
    pub message: String,
}

#[derive(Debug, Serialize, Clone, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum FileOutcome {
    Processed,
    SkippedDedup,
    Failed,
}
