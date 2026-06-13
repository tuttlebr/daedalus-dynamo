use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Instant;

use sha2::{Digest, Sha256};
use time::OffsetDateTime;
use tokio::fs;
use tokio::sync::Semaphore;

use crate::config::Config;
use crate::db::{self, DbPool, ReserveOutcome};
use crate::error::{AppError, AppResult};
use crate::jobs::JobState;
use crate::llm::LlmClient;
use crate::models::{PhotoMetadata, SCHEMA_VERSION};
use crate::transcode;

#[derive(Clone)]
pub struct Pipeline {
    pub cfg: Arc<Config>,
    pub pool: DbPool,
    pub llm: LlmClient,
    pub hash_sem: Arc<Semaphore>,
    pub llm_sem: Arc<Semaphore>,
}

impl Pipeline {
    pub async fn run_path(&self, path: PathBuf, job: Arc<JobState>) {
        let path_str = path.display().to_string();
        let started = Instant::now();
        job.note_seen();
        self.persist_job_best_effort(&job).await;

        match self.process_one(&path).await {
            Ok(Outcome::Processed { transcoded, bytes }) => {
                let dur = started.elapsed().as_millis() as u64;
                job.note_processed(transcoded, bytes, path_str, dur);
                self.persist_job_best_effort(&job).await;
            }
            Ok(Outcome::SkippedDedup) => {
                job.note_skipped_dedup(path_str);
                self.persist_job_best_effort(&job).await;
            }
            Err(e) => {
                let dur = started.elapsed().as_millis() as u64;
                tracing::warn!(path = %path.display(), error = %e, "file failed");
                self.record_path_failure_best_effort(&path, &e).await;
                job.note_failed(path_str, dur);
                self.persist_job_best_effort(&job).await;
            }
        }
    }

    pub async fn reprocess_path(&self, path: PathBuf, job: Arc<JobState>) -> AppResult<()> {
        let path_str = path.display().to_string();
        let started = Instant::now();
        job.note_seen();
        self.persist_job_best_effort(&job).await;
        match self.process_one_force(&path).await {
            Ok(Outcome::Processed { transcoded, bytes }) => {
                let dur = started.elapsed().as_millis() as u64;
                job.note_processed(transcoded, bytes, path_str, dur);
                self.persist_job_best_effort(&job).await;
            }
            Ok(Outcome::SkippedDedup) => {
                // Force mode skips the dedup gate, so this is defensive only.
                job.note_skipped_dedup(path_str);
                self.persist_job_best_effort(&job).await;
            }
            Err(e) => {
                let dur = started.elapsed().as_millis() as u64;
                tracing::warn!(path = %path.display(), error = %e, "reprocess failed");
                self.record_path_failure_best_effort(&path, &e).await;
                job.note_failed(path_str, dur);
                self.persist_job_best_effort(&job).await;
                return Err(e);
            }
        }
        Ok(())
    }

    async fn process_one(&self, path: &Path) -> AppResult<Outcome> {
        // 1. Read, hash, and prepare/transcode under one permit so scan load
        // does not saturate the blocking worker pool and starve HTTP probes.
        let work_permit = self
            .hash_sem
            .acquire()
            .await
            .map_err(|e| AppError::Internal(format!("hash semaphore closed: {e}")))?;

        let file_meta = fs::metadata(path).await?;
        let file_size = file_meta.len();
        ensure_file_size(file_size, self.cfg.max_file_bytes)?;
        let mtime_rfc = metadata_mtime_rfc3339(&file_meta).unwrap_or_else(now_rfc3339);
        let raw_bytes = fs::read(path).await?;

        let path_str = path.display().to_string();

        let sha = hex::encode(Sha256::digest(&raw_bytes));
        let guessed_mime = guess_original_mime(path);

        // 2. Reserve this content hash before spending an LLM call.
        let pool = self.pool.clone();
        let reserve_sha = sha.clone();
        let reserve_mime = guessed_mime.clone();
        let reserve_model = self.cfg.model_name.clone();
        match db::run_blocking(move || {
            db::reserve_processing(
                &pool,
                &reserve_sha,
                file_size,
                &reserve_mime,
                &reserve_model,
                false,
            )
        })
        .await?
        {
            ReserveOutcome::AlreadyProcessed | ReserveOutcome::AlreadyProcessing => {
                // Always update the path → sha mapping so renames are tracked.
                let pool = self.pool.clone();
                let upsert_path = path_str.clone();
                let upsert_sha = sha.clone();
                let upsert_mtime = mtime_rfc.clone();
                db::run_blocking(move || {
                    db::record_existing_path(&pool, &upsert_path, &upsert_sha, &upsert_mtime)
                })
                .await?;
                return Ok(Outcome::SkippedDedup);
            }
            ReserveOutcome::Reserved => {}
        }

        // 3. Format detection / transcode (CPU-bound, on blocking pool).
        let (prepared, original_mime) = match self.prepare(raw_bytes).await {
            Ok(prepared) => prepared,
            Err(e) => {
                let error_message = e.to_string();
                self.record_failed_file(
                    &path_str,
                    &sha,
                    &mtime_rfc,
                    file_size,
                    &guessed_mime,
                    &error_message,
                )
                .await?;
                return Err(e);
            }
        };
        drop(work_permit);

        // 4. LLM call (separate semaphore).
        let _llm_permit = self
            .llm_sem
            .acquire()
            .await
            .map_err(|e| AppError::Internal(format!("llm semaphore closed: {e}")))?;

        let llm_started = Instant::now();
        let llm_result = self.llm.analyze(&prepared).await;
        let llm_ms = llm_started.elapsed().as_millis() as u64;

        match llm_result {
            Ok(out) => {
                let processed_at = OffsetDateTime::now_utc()
                    .format(&time::format_description::well_known::Rfc3339)
                    .unwrap_or_default();

                let metadata = PhotoMetadata {
                    schema_version: SCHEMA_VERSION.to_string(),
                    source_path: path_str.clone(),
                    file_sha256: sha.clone(),
                    file_size_bytes: file_size,
                    file_mtime_rfc3339: mtime_rfc.clone(),
                    original_mime_type: original_mime.clone(),
                    width: prepared.width,
                    height: prepared.height,
                    processing_model: self.cfg.model_name.clone(),
                    model_version: out.model_version,
                    prompt_version: crate::llm::prompt_version().to_string(),
                    processed_at_rfc3339: processed_at,
                    processing_duration_ms: llm_ms,
                    raw_response: out.raw,
                    content: out.content,
                };

                let pool = self.pool.clone();
                let db_metadata = metadata.clone();
                let db_path = path_str.clone();
                let db_sha = sha.clone();
                let db_mtime = mtime_rfc.clone();
                db::run_blocking(move || {
                    db::record_processed_path(&pool, &db_metadata, &db_path, &db_sha, &db_mtime)
                })
                .await?;

                self.write_sidecar_if_enabled(path, &metadata).await;

                Ok(Outcome::Processed {
                    transcoded: prepared.transcoded,
                    bytes: file_size,
                })
            }
            Err(e) => {
                let pool = self.pool.clone();
                let failed_sha = sha.clone();
                let failed_mime = original_mime.clone();
                let failed_model = self.cfg.model_name.clone();
                let failed_error = e.to_string();
                let failed_path = path_str.clone();
                let failed_mtime = mtime_rfc.clone();
                db::run_blocking(move || {
                    db::record_failed_file_with_path(
                        &pool,
                        &failed_path,
                        &failed_sha,
                        &failed_mtime,
                        file_size,
                        &failed_mime,
                        &failed_model,
                        &failed_error,
                    )
                })
                .await?;
                Err(e)
            }
        }
    }

    async fn process_one_force(&self, path: &Path) -> AppResult<Outcome> {
        // Same as process_one but skips the dedup gate.
        let work_permit = self
            .hash_sem
            .acquire()
            .await
            .map_err(|e| AppError::Internal(format!("hash semaphore closed: {e}")))?;

        let file_meta = fs::metadata(path).await?;
        let file_size = file_meta.len();
        ensure_file_size(file_size, self.cfg.max_file_bytes)?;
        let mtime_rfc = metadata_mtime_rfc3339(&file_meta).unwrap_or_else(now_rfc3339);
        let raw_bytes = fs::read(path).await?;
        let path_str = path.display().to_string();

        let sha = hex::encode(Sha256::digest(&raw_bytes));
        let guessed_mime = guess_original_mime(path);

        let pool = self.pool.clone();
        let reserve_sha = sha.clone();
        let reserve_mime = guessed_mime.clone();
        let reserve_model = self.cfg.model_name.clone();
        if db::run_blocking(move || {
            db::reserve_processing(
                &pool,
                &reserve_sha,
                file_size,
                &reserve_mime,
                &reserve_model,
                true,
            )
        })
        .await?
            == ReserveOutcome::AlreadyProcessing
        {
            return Ok(Outcome::SkippedDedup);
        }

        let (prepared, original_mime) = match self.prepare(raw_bytes).await {
            Ok(prepared) => prepared,
            Err(e) => {
                let error_message = e.to_string();
                self.record_failed_file(
                    &path_str,
                    &sha,
                    &mtime_rfc,
                    file_size,
                    &guessed_mime,
                    &error_message,
                )
                .await?;
                return Err(e);
            }
        };
        drop(work_permit);

        let _llm_permit = self
            .llm_sem
            .acquire()
            .await
            .map_err(|e| AppError::Internal(format!("llm semaphore closed: {e}")))?;
        let llm_started = Instant::now();
        let llm_result = self.llm.analyze(&prepared).await;
        let llm_ms = llm_started.elapsed().as_millis() as u64;

        match llm_result {
            Ok(out) => {
                let metadata = PhotoMetadata {
                    schema_version: SCHEMA_VERSION.to_string(),
                    source_path: path_str.clone(),
                    file_sha256: sha.clone(),
                    file_size_bytes: file_size,
                    file_mtime_rfc3339: mtime_rfc.clone(),
                    original_mime_type: original_mime,
                    width: prepared.width,
                    height: prepared.height,
                    processing_model: self.cfg.model_name.clone(),
                    model_version: out.model_version,
                    prompt_version: crate::llm::prompt_version().to_string(),
                    processed_at_rfc3339: now_rfc3339(),
                    processing_duration_ms: llm_ms,
                    raw_response: out.raw,
                    content: out.content,
                };
                let pool = self.pool.clone();
                let db_metadata = metadata.clone();
                let db_path = path_str.clone();
                let db_sha = sha.clone();
                let db_mtime = mtime_rfc.clone();
                db::run_blocking(move || {
                    db::record_processed_path(&pool, &db_metadata, &db_path, &db_sha, &db_mtime)
                })
                .await?;

                self.write_sidecar_if_enabled(path, &metadata).await;

                Ok(Outcome::Processed {
                    transcoded: prepared.transcoded,
                    bytes: file_size,
                })
            }
            Err(e) => {
                let pool = self.pool.clone();
                let failed_sha = sha.clone();
                let failed_mime = original_mime.clone();
                let failed_model = self.cfg.model_name.clone();
                let failed_error = e.to_string();
                let failed_path = path_str.clone();
                let failed_mtime = mtime_rfc.clone();
                db::run_blocking(move || {
                    db::record_failed_file_with_path(
                        &pool,
                        &failed_path,
                        &failed_sha,
                        &failed_mtime,
                        file_size,
                        &failed_mime,
                        &failed_model,
                        &failed_error,
                    )
                })
                .await?;
                Err(e)
            }
        }
    }

    pub async fn persist_job(&self, job: &JobState) -> AppResult<()> {
        let pool = self.pool.clone();
        let summary = job.summary();
        db::run_blocking(move || db::upsert_job_summary(&pool, &summary)).await
    }

    async fn persist_job_best_effort(&self, job: &JobState) {
        if let Err(e) = self.persist_job(job).await {
            tracing::warn!(job_id = %job.id, error = %e, "job persistence failed");
        }
    }

    async fn record_failed_file(
        &self,
        path: &str,
        sha256: &str,
        file_mtime_rfc3339: &str,
        size_bytes: u64,
        original_mime: &str,
        error_message: &str,
    ) -> AppResult<()> {
        let pool = self.pool.clone();
        let failed_path = path.to_string();
        let failed_sha = sha256.to_string();
        let failed_mtime = file_mtime_rfc3339.to_string();
        let failed_mime = original_mime.to_string();
        let failed_model = self.cfg.model_name.clone();
        let failed_error = error_message.to_string();
        db::run_blocking(move || {
            db::record_failed_file_with_path(
                &pool,
                &failed_path,
                &failed_sha,
                &failed_mtime,
                size_bytes,
                &failed_mime,
                &failed_model,
                &failed_error,
            )
        })
        .await
    }

    async fn record_path_failure_best_effort(&self, path: &Path, error: &AppError) {
        let pool = self.pool.clone();
        let failed_path = path.display().to_string();
        let model = self.cfg.model_name.clone();
        let error_message = error.to_string();
        if let Err(e) = db::run_blocking(move || {
            db::record_path_failure(
                &pool,
                &failed_path,
                None,
                None,
                None,
                None,
                &model,
                &error_message,
            )
        })
        .await
        {
            tracing::warn!(path = %path.display(), error = %e, "failed-path persistence failed");
        }
    }

    async fn prepare(&self, raw: Vec<u8>) -> AppResult<(transcode::PreparedImage, String)> {
        let cfg = Arc::clone(&self.cfg);
        let prepared = tokio::task::spawn_blocking(move || {
            transcode::prepare_for_llm(
                &raw,
                cfg.max_edge_pixels,
                cfg.downscale_large,
                cfg.jpeg_quality,
                cfg.max_image_pixels,
            )
        })
        .await
        .map_err(|e| AppError::Internal(format!("transcode join error: {e}")))?
        .map_err(AppError::Llm)?;
        let original_mime = prepared.original_mime.clone();
        Ok((prepared, original_mime))
    }

    async fn write_sidecar_if_enabled(&self, path: &Path, metadata: &PhotoMetadata) {
        if !should_write_sidecar(&self.cfg, path) {
            return;
        }

        write_sidecar(path, metadata).await.unwrap_or_else(|e| {
            tracing::warn!(path = %path.display(), error = %e, "sidecar write failed");
        });
    }
}

#[derive(Debug)]
pub enum Outcome {
    Processed { transcoded: bool, bytes: u64 },
    SkippedDedup,
}

fn ensure_file_size(size: u64, max_size: u64) -> AppResult<()> {
    if size > max_size {
        return Err(AppError::PayloadTooLarge(format!(
            "file size {size} exceeds MAX_FILE_BYTES={max_size}"
        )));
    }
    Ok(())
}

fn metadata_mtime_rfc3339(meta: &std::fs::Metadata) -> Option<String> {
    let mtime = meta.modified().ok()?;
    let dt = OffsetDateTime::from(mtime);
    dt.format(&time::format_description::well_known::Rfc3339)
        .ok()
}

fn now_rfc3339() -> String {
    OffsetDateTime::now_utc()
        .format(&time::format_description::well_known::Rfc3339)
        .unwrap_or_default()
}

fn guess_original_mime(path: &Path) -> String {
    mime_guess::from_path(path)
        .first_raw()
        .unwrap_or("application/octet-stream")
        .to_string()
}

async fn write_sidecar(image_path: &Path, m: &PhotoMetadata) -> std::io::Result<()> {
    let mut sidecar = image_path.as_os_str().to_owned();
    sidecar.push(".metadata.json");
    let json = serde_json::to_vec_pretty(m)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
    fs::write(sidecar, json).await
}

fn should_write_sidecar(cfg: &Config, image_path: &Path) -> bool {
    cfg.write_sidecar
        && cfg
            .sidecar_roots
            .iter()
            .any(|root| image_path.starts_with(root))
}

pub fn validate_root(cfg: &Config, candidate: &Path) -> AppResult<PathBuf> {
    let canonical = std::fs::canonicalize(candidate).map_err(|e| {
        AppError::BadRequest(format!(
            "Path does not exist or is not accessible: {} ({})",
            candidate.display(),
            e
        ))
    })?;
    let allowed = cfg
        .allowed_roots
        .iter()
        .any(|root| canonical.starts_with(root));
    if !allowed {
        return Err(AppError::Forbidden(format!(
            "Path {} is outside allowed roots: {:?}",
            canonical.display(),
            cfg.allowed_roots
        )));
    }
    Ok(canonical)
}
