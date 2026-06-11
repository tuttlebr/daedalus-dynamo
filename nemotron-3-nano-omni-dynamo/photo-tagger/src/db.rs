use std::time::Duration;

use postgres::NoTls;
use r2d2::Pool;
use r2d2_postgres::PostgresConnectionManager;
use time::OffsetDateTime;

use crate::error::{AppError, AppResult};
use crate::models::{CountEntry, JobStatus, JobSummary, PhotoMetadata, SearchHit, StatsResponse};

pub type DbPool = Pool<PostgresConnectionManager<NoTls>>;

pub async fn run_blocking<T, F>(f: F) -> AppResult<T>
where
    T: Send + 'static,
    F: FnOnce() -> AppResult<T> + Send + 'static,
{
    tokio::task::spawn_blocking(f)
        .await
        .map_err(|e| AppError::Internal(format!("database task join error: {e}")))?
}

pub fn open_pool(database_url: &str) -> AppResult<DbPool> {
    let config = database_url
        .parse()
        .map_err(|e| AppError::Internal(format!("invalid DATABASE_URL: {e}")))?;
    let manager = PostgresConnectionManager::new(config, NoTls);
    let pool = Pool::builder()
        .max_size(16)
        .connection_timeout(Duration::from_secs(10))
        .build(manager)
        .map_err(AppError::Pool)?;

    let mut conn = pool.get()?;
    conn.batch_execute(SCHEMA_SQL)?;
    mark_interrupted_work(&mut conn)?;
    tracing::info!("Postgres ready");
    Ok(pool)
}

pub fn ping(pool: &DbPool) -> AppResult<()> {
    let mut conn = pool.get()?;
    conn.simple_query("SELECT 1")?;
    Ok(())
}

const SCHEMA_SQL: &str = r#"
CREATE TABLE IF NOT EXISTS files (
    sha256          TEXT PRIMARY KEY,
    size_bytes      BIGINT NOT NULL,
    width           INTEGER,
    height          INTEGER,
    original_mime   TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('processing','processed','failed')),
    schema_version  TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    model_name      TEXT NOT NULL,
    metadata_json   TEXT,
    error_message   TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    processing_ms   BIGINT,
    processed_at    TEXT NOT NULL,
    search_text     TEXT NOT NULL DEFAULT ''
);

ALTER TABLE files ADD COLUMN IF NOT EXISTS search_text TEXT NOT NULL DEFAULT '';
ALTER TABLE files DROP CONSTRAINT IF EXISTS files_status_check;
ALTER TABLE files ADD CONSTRAINT files_status_check
    CHECK (status IN ('processing','processed','failed'));

CREATE INDEX IF NOT EXISTS files_status_idx     ON files(status);
CREATE INDEX IF NOT EXISTS files_processed_idx  ON files(processed_at DESC);
CREATE INDEX IF NOT EXISTS files_search_idx     ON files USING GIN (to_tsvector('simple', search_text));

CREATE TABLE IF NOT EXISTS paths (
    path         TEXT PRIMARY KEY,
    sha256       TEXT NOT NULL REFERENCES files(sha256) ON DELETE CASCADE,
    file_mtime   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS paths_sha256_idx ON paths(sha256);

CREATE TABLE IF NOT EXISTS failed_paths (
    path          TEXT PRIMARY KEY,
    sha256        TEXT,
    file_mtime    TEXT,
    size_bytes    BIGINT,
    original_mime TEXT,
    model_name    TEXT NOT NULL,
    error_message TEXT NOT NULL,
    failed_at     TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS failed_paths_failed_idx ON failed_paths(failed_at DESC);

CREATE TABLE IF NOT EXISTS jobs (
    id             TEXT PRIMARY KEY,
    root           TEXT NOT NULL,
    recursive      BOOLEAN NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('running','completed','completed_with_failures','failed')),
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    total_seen     BIGINT NOT NULL DEFAULT 0,
    processed      BIGINT NOT NULL DEFAULT 0,
    skipped_dedup  BIGINT NOT NULL DEFAULT 0,
    failed         BIGINT NOT NULL DEFAULT 0,
    transcoded     BIGINT NOT NULL DEFAULT 0,
    bytes_seen     BIGINT NOT NULL DEFAULT 0,
    current_path   TEXT,
    message        TEXT
);
CREATE INDEX IF NOT EXISTS jobs_started_idx ON jobs(started_at DESC);
"#;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReserveOutcome {
    Reserved,
    AlreadyProcessed,
    AlreadyProcessing,
}

fn mark_interrupted_work(conn: &mut postgres::Client) -> AppResult<()> {
    let now = now_rfc3339();
    conn.execute(
        "UPDATE jobs
         SET status='failed',
             finished_at=$1,
             message=COALESCE(message, 'service restarted before job completed')
         WHERE status='running'",
        &[&now],
    )?;
    conn.execute(
        "INSERT INTO failed_paths(path, sha256, file_mtime, size_bytes, original_mime,
                                  model_name, error_message, failed_at, attempts)
         SELECT paths.path, files.sha256, paths.file_mtime, files.size_bytes,
                files.original_mime, files.model_name,
                'service restarted while processing this file', $1, 1
         FROM paths
         JOIN files ON files.sha256 = paths.sha256
         WHERE files.status='processing'
         ON CONFLICT(path) DO UPDATE SET
            sha256=excluded.sha256,
            file_mtime=excluded.file_mtime,
            size_bytes=excluded.size_bytes,
            original_mime=excluded.original_mime,
            model_name=excluded.model_name,
            error_message=excluded.error_message,
            failed_at=excluded.failed_at,
            attempts=failed_paths.attempts + 1",
        &[&now],
    )?;
    conn.execute(
        "UPDATE files
         SET status='failed',
             error_message='service restarted while processing this file',
             processed_at=$1
         WHERE status='processing'",
        &[&now],
    )?;
    Ok(())
}

pub fn reserve_processing(
    pool: &DbPool,
    sha256: &str,
    size_bytes: u64,
    original_mime: &str,
    model_name: &str,
    force: bool,
) -> AppResult<ReserveOutcome> {
    let mut conn = pool.get()?;
    let now = now_rfc3339();
    let size_bytes = size_bytes as i64;
    let mut tx = conn.transaction()?;

    let inserted = tx.execute(
        "INSERT INTO files(sha256, size_bytes, original_mime, status, schema_version,
                           prompt_version, model_name, attempts, processed_at, search_text)
         VALUES($1,$2,$3,'processing',$4,$5,$6,0,$7,'')
         ON CONFLICT(sha256) DO NOTHING",
        &[
            &sha256,
            &size_bytes,
            &original_mime,
            &crate::models::SCHEMA_VERSION,
            &crate::models::PROMPT_VERSION,
            &model_name,
            &now,
        ],
    )?;
    if inserted == 1 {
        tx.commit()?;
        return Ok(ReserveOutcome::Reserved);
    }

    let status: String = tx
        .query_one(
            "SELECT status FROM files WHERE sha256 = $1 FOR UPDATE",
            &[&sha256],
        )?
        .get(0);
    let outcome = match status.as_str() {
        "processed" if !force => ReserveOutcome::AlreadyProcessed,
        "processing" => ReserveOutcome::AlreadyProcessing,
        "processed" | "failed" => {
            tx.execute(
                "UPDATE files
                 SET size_bytes=$2,
                     original_mime=$3,
                     status='processing',
                     schema_version=$4,
                     prompt_version=$5,
                     model_name=$6,
                     error_message=NULL,
                     processed_at=$7
                 WHERE sha256=$1",
                &[
                    &sha256,
                    &size_bytes,
                    &original_mime,
                    &crate::models::SCHEMA_VERSION,
                    &crate::models::PROMPT_VERSION,
                    &model_name,
                    &now,
                ],
            )?;
            ReserveOutcome::Reserved
        }
        _ => ReserveOutcome::AlreadyProcessing,
    };
    tx.commit()?;
    Ok(outcome)
}

pub fn record_existing_path(
    pool: &DbPool,
    path: &str,
    sha256: &str,
    file_mtime_rfc3339: &str,
) -> AppResult<()> {
    let mut conn = pool.get()?;
    let now = now_rfc3339();
    let mut tx = conn.transaction()?;
    tx.execute(
        "INSERT INTO paths(path, sha256, file_mtime, last_seen_at)
         VALUES($1, $2, $3, $4)
         ON CONFLICT(path) DO UPDATE SET
            sha256=excluded.sha256,
            file_mtime=excluded.file_mtime,
            last_seen_at=excluded.last_seen_at",
        &[&path, &sha256, &file_mtime_rfc3339, &now],
    )?;
    tx.execute("DELETE FROM failed_paths WHERE path=$1", &[&path])?;
    tx.commit()?;
    Ok(())
}

pub fn record_processed_path(
    pool: &DbPool,
    m: &PhotoMetadata,
    path: &str,
    sha256: &str,
    file_mtime_rfc3339: &str,
) -> AppResult<()> {
    let mut conn = pool.get()?;
    let metadata_json = serde_json::to_string(m)?;
    let search_text = build_search_text(m);
    let size_bytes = m.file_size_bytes as i64;
    let width = m.width as i32;
    let height = m.height as i32;
    let processing_ms = m.processing_duration_ms as i64;
    let now = now_rfc3339();

    let mut tx = conn.transaction()?;
    tx.execute(
        "INSERT INTO files(sha256, size_bytes, width, height, original_mime, status,
                            schema_version, prompt_version, model_name, metadata_json,
                            error_message, attempts, processing_ms, processed_at, search_text)
         VALUES($1,$2,$3,$4,$5,'processed',$6,$7,$8,$9,NULL,1,$10,$11,$12)
         ON CONFLICT(sha256) DO UPDATE SET
            size_bytes=excluded.size_bytes,
            width=excluded.width,
            height=excluded.height,
            original_mime=excluded.original_mime,
            status='processed',
            schema_version=excluded.schema_version,
            prompt_version=excluded.prompt_version,
            model_name=excluded.model_name,
            metadata_json=excluded.metadata_json,
            error_message=NULL,
            attempts=files.attempts + 1,
            processing_ms=excluded.processing_ms,
            processed_at=excluded.processed_at,
            search_text=excluded.search_text",
        &[
            &m.file_sha256,
            &size_bytes,
            &width,
            &height,
            &m.original_mime_type,
            &m.schema_version,
            &m.prompt_version,
            &m.processing_model,
            &metadata_json,
            &processing_ms,
            &m.processed_at_rfc3339,
            &search_text,
        ],
    )?;
    tx.execute(
        "INSERT INTO paths(path, sha256, file_mtime, last_seen_at)
         VALUES($1, $2, $3, $4)
         ON CONFLICT(path) DO UPDATE SET
            sha256=excluded.sha256,
            file_mtime=excluded.file_mtime,
            last_seen_at=excluded.last_seen_at",
        &[&path, &sha256, &file_mtime_rfc3339, &now],
    )?;
    tx.execute("DELETE FROM failed_paths WHERE path=$1", &[&path])?;
    tx.commit()?;
    Ok(())
}

pub fn record_failed_file_with_path(
    pool: &DbPool,
    path: &str,
    sha256: &str,
    file_mtime_rfc3339: &str,
    size_bytes: u64,
    original_mime: &str,
    model_name: &str,
    error_message: &str,
) -> AppResult<()> {
    let mut conn = pool.get()?;
    let now = now_rfc3339();
    let size_bytes = size_bytes as i64;
    let mut tx = conn.transaction()?;
    tx.execute(
        "INSERT INTO files(sha256, size_bytes, original_mime, status, schema_version,
                           prompt_version, model_name, error_message, attempts, processed_at, search_text)
         VALUES($1,$2,$3,'failed',$4,$5,$6,$7,1,$8,'')
         ON CONFLICT(sha256) DO UPDATE SET
            size_bytes=excluded.size_bytes,
            original_mime=excluded.original_mime,
            status='failed',
            schema_version=excluded.schema_version,
            prompt_version=excluded.prompt_version,
            model_name=excluded.model_name,
            metadata_json=NULL,
            error_message=excluded.error_message,
            attempts=files.attempts + 1,
            processed_at=excluded.processed_at,
            search_text=''",
        &[
            &sha256,
            &size_bytes,
            &original_mime,
            &crate::models::SCHEMA_VERSION,
            &crate::models::PROMPT_VERSION,
            &model_name,
            &error_message,
            &now,
        ],
    )?;
    tx.execute(
        "INSERT INTO paths(path, sha256, file_mtime, last_seen_at)
         VALUES($1, $2, $3, $4)
         ON CONFLICT(path) DO UPDATE SET
            sha256=excluded.sha256,
            file_mtime=excluded.file_mtime,
            last_seen_at=excluded.last_seen_at",
        &[&path, &sha256, &file_mtime_rfc3339, &now],
    )?;
    tx.commit()?;
    Ok(())
}

pub fn record_path_failure(
    pool: &DbPool,
    path: &str,
    sha256: Option<&str>,
    file_mtime_rfc3339: Option<&str>,
    size_bytes: Option<u64>,
    original_mime: Option<&str>,
    model_name: &str,
    error_message: &str,
) -> AppResult<()> {
    let mut conn = pool.get()?;
    let now = now_rfc3339();
    let size_bytes = size_bytes.map(|value| value as i64);
    conn.execute(
        "INSERT INTO failed_paths(path, sha256, file_mtime, size_bytes, original_mime,
                                  model_name, error_message, failed_at, attempts)
         VALUES($1,$2,$3,$4,$5,$6,$7,$8,1)
         ON CONFLICT(path) DO UPDATE SET
            sha256=excluded.sha256,
            file_mtime=excluded.file_mtime,
            size_bytes=excluded.size_bytes,
            original_mime=excluded.original_mime,
            model_name=excluded.model_name,
            error_message=excluded.error_message,
            failed_at=excluded.failed_at,
            attempts=failed_paths.attempts + 1",
        &[
            &path,
            &sha256,
            &file_mtime_rfc3339,
            &size_bytes,
            &original_mime,
            &model_name,
            &error_message,
            &now,
        ],
    )?;
    Ok(())
}

pub fn get_metadata_by_path(pool: &DbPool, path: &str) -> AppResult<Option<PhotoMetadata>> {
    let mut conn = pool.get()?;
    let json: Option<String> = conn
        .query_opt(
            "SELECT files.metadata_json
             FROM paths
             JOIN files ON files.sha256 = paths.sha256
             WHERE paths.path = $1 AND files.status = 'processed'",
            &[&path],
        )?
        .map(|r| r.get(0));
    Ok(json.and_then(|j| serde_json::from_str(&j).ok()))
}

pub fn search_fts(pool: &DbPool, query: &str, limit: usize) -> AppResult<Vec<SearchHit>> {
    let safe_query = sanitize_search_query(query);
    if safe_query.is_empty() {
        return Ok(Vec::new());
    }

    let mut conn = pool.get()?;
    let limit = limit as i64;
    let rows = conn.query(
        "WITH q AS (SELECT plainto_tsquery('simple', $1) AS query)
         SELECT files.sha256,
                COALESCE((SELECT path FROM paths WHERE paths.sha256 = files.sha256 ORDER BY last_seen_at DESC LIMIT 1), '') AS path,
                files.metadata_json,
                files.processed_at
         FROM files, q
         WHERE files.status = 'processed'
           AND to_tsvector('simple', files.search_text) @@ q.query
         ORDER BY ts_rank(to_tsvector('simple', files.search_text), q.query) DESC,
                  files.processed_at DESC
         LIMIT $2",
        &[&safe_query, &limit],
    )?;

    rows.into_iter()
        .map(search_hit_from_row)
        .collect::<AppResult<Vec<_>>>()
}

pub fn recent_processed(pool: &DbPool, limit: usize) -> AppResult<Vec<SearchHit>> {
    let mut conn = pool.get()?;
    let limit = limit as i64;
    let rows = conn.query(
        "SELECT files.sha256,
                COALESCE((SELECT path FROM paths WHERE paths.sha256 = files.sha256 ORDER BY last_seen_at DESC LIMIT 1), '') AS path,
                files.metadata_json,
                files.processed_at
         FROM files
         WHERE files.status = 'processed'
         ORDER BY files.processed_at DESC
         LIMIT $1",
        &[&limit],
    )?;

    rows.into_iter()
        .map(search_hit_from_row)
        .collect::<AppResult<Vec<_>>>()
}

pub fn stats(pool: &DbPool) -> AppResult<StatsResponse> {
    let mut conn = pool.get()?;

    let total: u64 = conn
        .query_one("SELECT COUNT(*) FROM files", &[])?
        .get::<_, i64>(0) as u64;
    let processed: u64 = conn
        .query_one("SELECT COUNT(*) FROM files WHERE status='processed'", &[])?
        .get::<_, i64>(0) as u64;
    let failed: u64 = conn
        .query_one("SELECT COUNT(*) FROM files WHERE status='failed'", &[])?
        .get::<_, i64>(0) as u64;
    let failed_paths: u64 = conn
        .query_one("SELECT COUNT(*) FROM failed_paths", &[])?
        .get::<_, i64>(0) as u64;
    let paths: u64 = conn
        .query_one("SELECT COUNT(*) FROM paths", &[])?
        .get::<_, i64>(0) as u64;

    let by_model = conn
        .query(
            "SELECT model_name, COUNT(*) FROM files GROUP BY model_name ORDER BY 2 DESC",
            &[],
        )?
        .into_iter()
        .map(|r| CountEntry {
            key: r.get(0),
            count: r.get::<_, i64>(1) as u64,
        })
        .collect();

    let by_setting_type = conn
        .query(
            "SELECT COALESCE(NULLIF(metadata_json::jsonb #>> '{content,setting_type}', ''), 'unknown') AS k,
                    COUNT(*)
             FROM files
             WHERE status='processed'
             GROUP BY k
             ORDER BY 2 DESC",
            &[],
        )?
        .into_iter()
        .map(|r| CountEntry {
            key: r.get(0),
            count: r.get::<_, i64>(1) as u64,
        })
        .collect();

    Ok(StatsResponse {
        total,
        processed,
        failed: failed.max(failed_paths),
        failed_paths,
        paths,
        by_setting_type,
        by_model,
    })
}

pub fn upsert_job_summary(pool: &DbPool, summary: &JobSummary) -> AppResult<()> {
    let mut conn = pool.get()?;
    let id = summary.id.to_string();
    let total_seen = summary.total_seen as i64;
    let processed = summary.processed as i64;
    let skipped_dedup = summary.skipped_dedup as i64;
    let failed = summary.failed as i64;
    let transcoded = summary.transcoded as i64;
    let bytes_seen = summary.bytes_seen as i64;
    let status = summary.status.as_str();
    conn.execute(
        "INSERT INTO jobs(id, root, recursive, status, started_at, finished_at,
                          total_seen, processed, skipped_dedup, failed, transcoded,
                          bytes_seen, current_path, message)
         VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
         ON CONFLICT(id) DO UPDATE SET
            root=excluded.root,
            recursive=excluded.recursive,
            status=excluded.status,
            started_at=excluded.started_at,
            finished_at=excluded.finished_at,
            total_seen=excluded.total_seen,
            processed=excluded.processed,
            skipped_dedup=excluded.skipped_dedup,
            failed=excluded.failed,
            transcoded=excluded.transcoded,
            bytes_seen=excluded.bytes_seen,
            current_path=excluded.current_path,
            message=excluded.message",
        &[
            &id,
            &summary.root,
            &summary.recursive,
            &status,
            &summary.started_at,
            &summary.finished_at,
            &total_seen,
            &processed,
            &skipped_dedup,
            &failed,
            &transcoded,
            &bytes_seen,
            &summary.current_path,
            &summary.message,
        ],
    )?;
    Ok(())
}

pub fn list_jobs(pool: &DbPool) -> AppResult<Vec<JobSummary>> {
    let mut conn = pool.get()?;
    let rows = conn.query(
        "SELECT id, root, recursive, status, started_at, finished_at,
                total_seen, processed, skipped_dedup, failed, transcoded,
                bytes_seen, current_path, message
         FROM jobs
         ORDER BY started_at DESC
         LIMIT 200",
        &[],
    )?;
    rows.into_iter().map(job_summary_from_row).collect()
}

pub fn get_job_summary(pool: &DbPool, id: uuid::Uuid) -> AppResult<Option<JobSummary>> {
    let mut conn = pool.get()?;
    let id = id.to_string();
    conn.query_opt(
        "SELECT id, root, recursive, status, started_at, finished_at,
                total_seen, processed, skipped_dedup, failed, transcoded,
                bytes_seen, current_path, message
         FROM jobs
         WHERE id=$1",
        &[&id],
    )?
    .map(job_summary_from_row)
    .transpose()
}

pub fn processed_count(pool: &DbPool) -> AppResult<u64> {
    let mut conn = pool.get()?;
    Ok(conn
        .query_one("SELECT COUNT(*) FROM files WHERE status='processed'", &[])?
        .get::<_, i64>(0) as u64)
}

fn job_summary_from_row(row: postgres::Row) -> AppResult<JobSummary> {
    let id: String = row.get(0);
    let status: String = row.get(3);
    let id = uuid::Uuid::parse_str(&id)
        .map_err(|e| AppError::Internal(format!("invalid persisted job id: {e}")))?;
    let status = JobStatus::from_str(&status)
        .ok_or_else(|| AppError::Internal(format!("invalid persisted job status: {status}")))?;
    Ok(JobSummary {
        id,
        root: row.get(1),
        recursive: row.get(2),
        status,
        started_at: row.get(4),
        finished_at: row.get(5),
        total_seen: row.get::<_, i64>(6) as u64,
        processed: row.get::<_, i64>(7) as u64,
        skipped_dedup: row.get::<_, i64>(8) as u64,
        failed: row.get::<_, i64>(9) as u64,
        transcoded: row.get::<_, i64>(10) as u64,
        bytes_seen: row.get::<_, i64>(11) as u64,
        current_path: row.get(12),
        message: row.get(13),
    })
}

fn search_hit_from_row(row: postgres::Row) -> AppResult<SearchHit> {
    let sha256: String = row.get(0);
    let path: String = row.get(1);
    let json: Option<String> = row.get(2);
    let processed_at: String = row.get(3);
    let (caption, tags, setting_type) = extract_summary(json.as_deref());
    Ok(SearchHit {
        sha256,
        path,
        caption,
        tags,
        setting_type,
        processed_at,
    })
}

fn extract_summary(json: Option<&str>) -> (String, Vec<String>, String) {
    if let Some(j) = json {
        if let Ok(m) = serde_json::from_str::<PhotoMetadata>(j) {
            return (m.content.caption, m.content.tags, m.content.setting_type);
        }
    }
    (String::new(), Vec::new(), String::new())
}

fn build_search_text(m: &PhotoMetadata) -> String {
    [
        m.content.caption.as_str(),
        m.content.description.as_str(),
        m.content.scene.as_str(),
        &m.content.tags.join(" "),
        &m.content.detected_text.join(" "),
        &m.content.objects.join(" "),
    ]
    .join(" ")
}

fn sanitize_search_query(query: &str) -> String {
    query
        .split_whitespace()
        .map(|t| {
            t.chars()
                .filter(|c| c.is_alphanumeric() || *c == '-' || *c == '_')
                .collect::<String>()
        })
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

pub fn now_rfc3339() -> String {
    OffsetDateTime::now_utc()
        .format(&time::format_description::well_known::Rfc3339)
        .unwrap_or_default()
}
