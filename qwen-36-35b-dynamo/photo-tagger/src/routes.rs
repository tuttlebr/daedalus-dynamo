use std::convert::Infallible;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use axum::extract::Request;
use axum::extract::{Query, State};
use axum::http::{header, HeaderMap, HeaderValue, StatusCode};
use axum::middleware::Next;
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::{IntoResponse, Response};
use axum::Json;
use base64::Engine;
use futures::stream::Stream;
use tokio::sync::mpsc;
use tokio_stream::wrappers::BroadcastStream;
use tokio_stream::StreamExt as _;
use uuid::Uuid;

use crate::config::Config;
use crate::db;
use crate::error::{AppError, AppResult};
use crate::jobs::JobRegistry;
use crate::models::{
    BrowseResponse, HealthResponse, JobEvent, JobStatus, JobSummary, PhotoMetadata, ReadyResponse,
    ReprocessRequest, ReprocessResponse, ScanRequest, ScanResponse, SearchResponse, StatsResponse,
};
use crate::pipeline::{validate_root, Pipeline};
use crate::walker::{self, WalkConfig};

pub struct AppState {
    pub pipeline: Pipeline,
    pub jobs: Arc<JobRegistry>,
    pub database_ready: Arc<AtomicBool>,
    pub endpoint_reachable: Arc<AtomicBool>,
}

pub async fn require_auth(
    State(state): State<Arc<AppState>>,
    req: Request,
    next: Next,
) -> Result<Response, Response> {
    let cfg = &state.pipeline.cfg;
    if !cfg.require_auth {
        return Ok(next.run(req).await);
    }
    if is_authorized(req.headers(), cfg) {
        return Ok(next.run(req).await);
    }

    let mut response = (
        StatusCode::UNAUTHORIZED,
        Json(serde_json::json!({ "error": "authentication required" })),
    )
        .into_response();
    response.headers_mut().insert(
        header::WWW_AUTHENTICATE,
        HeaderValue::from_static("Basic realm=\"Photo Tagger\""),
    );
    Err(response)
}

fn is_authorized(headers: &HeaderMap, cfg: &Config) -> bool {
    let Some(token) = cfg.auth_token.as_deref() else {
        return false;
    };

    if headers
        .get("x-photo-tagger-token")
        .and_then(|value| value.to_str().ok())
        .map(|value| constant_time_eq(value.as_bytes(), token.as_bytes()))
        .unwrap_or(false)
    {
        return true;
    }

    let Some(auth) = headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
    else {
        return false;
    };

    if let Some(bearer) = auth.strip_prefix("Bearer ") {
        return constant_time_eq(bearer.trim().as_bytes(), token.as_bytes());
    }

    if let Some(encoded) = auth.strip_prefix("Basic ") {
        let Ok(decoded) = base64::engine::general_purpose::STANDARD.decode(encoded.trim()) else {
            return false;
        };
        let Ok(decoded) = String::from_utf8(decoded) else {
            return false;
        };
        let Some((user, password)) = decoded.split_once(':') else {
            return false;
        };
        return constant_time_eq(user.as_bytes(), cfg.auth_user.as_bytes())
            && constant_time_eq(password.as_bytes(), token.as_bytes());
    }

    false
}

fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter()
        .zip(b.iter())
        .fold(0u8, |acc, (left, right)| acc | (left ^ right))
        == 0
}

// ----------------------------------------------------------------------
// /health  — liveness: process is up. Never makes network calls so it
// always answers within the kubelet's probe timeout.
// ----------------------------------------------------------------------
pub async fn health() -> Json<HealthResponse> {
    Json(HealthResponse { status: "ok" })
}

// ----------------------------------------------------------------------
// /ready  — readiness: database opened at startup + LLM endpoint reachable.
// Keep this request path free of filesystem/database I/O; storage latency here can
// block Tokio workers and make liveness probes queue behind readiness checks.
// ----------------------------------------------------------------------
pub async fn ready(State(state): State<Arc<AppState>>) -> (StatusCode, Json<ReadyResponse>) {
    let database_ok = state.database_ready.load(Ordering::Relaxed);
    let endpoint_reachable = state.endpoint_reachable.load(Ordering::Relaxed);

    let ready = database_ok && endpoint_reachable;
    let code = if ready {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    };

    (
        code,
        Json(ReadyResponse {
            status: if ready { "ready" } else { "not_ready" },
            database: database_ok,
            endpoint_reachable,
        }),
    )
}

// ----------------------------------------------------------------------
// POST /scan { path, recursive } -> { job_id }
// ----------------------------------------------------------------------
pub async fn start_scan(
    State(state): State<Arc<AppState>>,
    Json(req): Json<ScanRequest>,
) -> AppResult<Json<ScanResponse>> {
    let canonical =
        validate_root_async(Arc::clone(&state.pipeline.cfg), PathBuf::from(&req.path)).await?;
    if !tokio::fs::metadata(&canonical).await?.is_dir() {
        return Err(AppError::BadRequest(format!(
            "Path is not a directory: {}",
            canonical.display()
        )));
    }

    let job = state
        .jobs
        .create(canonical.display().to_string(), req.recursive);
    let job_id = job.id;
    state.pipeline.persist_job(&job).await?;

    let pipeline = state.pipeline.clone();
    let jobs = Arc::clone(&state.jobs);

    tokio::spawn(async move {
        let cfg = WalkConfig {
            root: canonical,
            recursive: req.recursive,
        };

        let buffer = (pipeline.cfg.max_concurrent_hash * 4).max(32);
        let (tx, mut rx) = mpsc::channel::<PathBuf>(buffer);

        let walk_task = tokio::spawn(walker::walk_into(cfg, tx));

        let mut handles = Vec::new();
        let mut task_errors = 0u64;
        while let Some(path) = rx.recv().await {
            let pl = pipeline.clone();
            let jb = Arc::clone(&job);
            handles.push(tokio::spawn(async move {
                pl.run_path(path, jb).await;
            }));
            // Apply soft cap on outstanding spawned tasks to bound memory.
            if handles.len() >= pipeline.cfg.max_concurrent_hash * 8 {
                for h in handles.drain(..) {
                    if h.await.is_err() {
                        task_errors += 1;
                    }
                }
            }
        }
        for h in handles {
            if h.await.is_err() {
                task_errors += 1;
            }
        }
        let walk_errors = match walk_task.await {
            Ok(Ok(summary)) => summary.errors,
            Ok(Err(err)) => {
                job.finish(JobStatus::Failed, Some(err));
                if let Err(e) = pipeline.persist_job(&job).await {
                    tracing::warn!(job_id = %job.id, error = %e, "job persistence failed");
                }
                jobs.evict_stale();
                return;
            }
            Err(err) => {
                job.finish(
                    JobStatus::Failed,
                    Some(format!("walk task join error: {err}")),
                );
                if let Err(e) = pipeline.persist_job(&job).await {
                    tracing::warn!(job_id = %job.id, error = %e, "job persistence failed");
                }
                jobs.evict_stale();
                return;
            }
        };
        job.note_walk_errors(walk_errors + task_errors);
        if let Err(e) = pipeline.persist_job(&job).await {
            tracing::warn!(job_id = %job.id, error = %e, "job persistence failed");
        }
        finish_job(&job);
        if let Err(e) = pipeline.persist_job(&job).await {
            tracing::warn!(job_id = %job.id, error = %e, "job persistence failed");
        }
        jobs.evict_stale();
    });

    Ok(Json(ScanResponse { job_id }))
}

// ----------------------------------------------------------------------
// POST /reprocess { path } -> { job_id }
// ----------------------------------------------------------------------
pub async fn reprocess(
    State(state): State<Arc<AppState>>,
    Json(req): Json<ReprocessRequest>,
) -> AppResult<Json<ReprocessResponse>> {
    let canonical =
        validate_root_async(Arc::clone(&state.pipeline.cfg), PathBuf::from(&req.path)).await?;
    if !tokio::fs::metadata(&canonical).await?.is_file() {
        return Err(AppError::BadRequest(format!(
            "Path is not a file: {}",
            canonical.display()
        )));
    }

    let job = state.jobs.create(canonical.display().to_string(), false);
    let job_id = job.id;
    state.pipeline.persist_job(&job).await?;
    let pipeline = state.pipeline.clone();
    let jobs = Arc::clone(&state.jobs);

    tokio::spawn(async move {
        if let Err(e) = pipeline.reprocess_path(canonical, Arc::clone(&job)).await {
            job.finish(JobStatus::Failed, Some(e.to_string()));
        } else {
            finish_job(&job);
        }
        if let Err(e) = pipeline.persist_job(&job).await {
            tracing::warn!(job_id = %job.id, error = %e, "job persistence failed");
        }
        jobs.evict_stale();
    });

    Ok(Json(ReprocessResponse { job_id }))
}

// ----------------------------------------------------------------------
// GET /jobs
// ----------------------------------------------------------------------
pub async fn list_jobs(State(state): State<Arc<AppState>>) -> AppResult<Json<Vec<JobSummary>>> {
    state.jobs.evict_stale();
    let pool = state.pipeline.pool.clone();
    Ok(Json(db::run_blocking(move || db::list_jobs(&pool)).await?))
}

// ----------------------------------------------------------------------
// GET /jobs/:id
// ----------------------------------------------------------------------
pub async fn get_job(
    State(state): State<Arc<AppState>>,
    axum::extract::Path(id): axum::extract::Path<Uuid>,
) -> AppResult<Json<JobSummary>> {
    if let Some(job) = state.jobs.get(id) {
        return Ok(Json(job.summary()));
    }
    let pool = state.pipeline.pool.clone();
    let summary = db::run_blocking(move || db::get_job_summary(&pool, id))
        .await?
        .ok_or_else(|| AppError::NotFound(format!("Unknown job: {id}")))?;
    Ok(Json(summary))
}

// ----------------------------------------------------------------------
// GET /jobs/:id/events  (SSE)
// ----------------------------------------------------------------------
pub async fn job_events(
    State(state): State<Arc<AppState>>,
    axum::extract::Path(id): axum::extract::Path<Uuid>,
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, AppError> {
    let job = state.jobs.get(id);
    let initial = if let Some(job) = &job {
        job.summary()
    } else {
        let pool = state.pipeline.pool.clone();
        db::run_blocking(move || db::get_job_summary(&pool, id))
            .await?
            .ok_or_else(|| AppError::NotFound(format!("Unknown job: {id}")))?
    };
    let rx = job.as_ref().map(|job| job.subscribe());

    // Send an initial snapshot so a late subscriber gets current state.
    let stream = async_stream::stream! {
        yield Ok::<_, Infallible>(event_for(&JobEvent::Progress(initial.clone())));
        if rx.is_none() {
            match initial.status {
                JobStatus::Completed | JobStatus::CompletedWithFailures => {
                    yield Ok::<_, Infallible>(event_for(&JobEvent::Completed(crate::models::JobCompleted {
                        id,
                        summary: initial,
                    })));
                }
                JobStatus::Failed => {
                    yield Ok::<_, Infallible>(event_for(&JobEvent::Failed(crate::models::JobFailed {
                        id,
                        message: initial.message.unwrap_or_else(|| "job failed".into()),
                    })));
                }
                JobStatus::Running => {}
            }
        } else if let Some(rx) = rx {
            let mut s = BroadcastStream::new(rx);
            while let Some(item) = s.next().await {
                if let Ok(event) = item {
                    yield Ok::<_, Infallible>(event_for(&event));
                }
            }
        }
    };

    Ok(Sse::new(stream).keep_alive(KeepAlive::new().interval(Duration::from_secs(15))))
}

fn event_for(ev: &JobEvent) -> Event {
    let (evname, payload) = match ev {
        JobEvent::Started(p) => ("started", serde_json::to_value(p)),
        JobEvent::File(p) => ("file", serde_json::to_value(p)),
        JobEvent::Progress(p) => ("progress", serde_json::to_value(p)),
        JobEvent::Completed(p) => ("completed", serde_json::to_value(p)),
        JobEvent::Failed(p) => ("failed", serde_json::to_value(p)),
    };
    let mut event = Event::default().event(evname);
    if let Ok(v) = payload {
        event = event
            .json_data(v)
            .unwrap_or_else(|_| Event::default().event(evname).data(""));
    }
    event
}

// ----------------------------------------------------------------------
// GET /metadata?path=...
// ----------------------------------------------------------------------
#[derive(serde::Deserialize)]
pub struct PathQuery {
    pub path: String,
}

pub async fn get_metadata(
    State(state): State<Arc<AppState>>,
    Query(q): Query<PathQuery>,
) -> AppResult<Json<PhotoMetadata>> {
    let canonical =
        validate_root_async(Arc::clone(&state.pipeline.cfg), PathBuf::from(&q.path)).await?;
    let path_str = canonical.display().to_string();
    let pool = state.pipeline.pool.clone();
    let db_path = path_str.clone();
    let m = db::run_blocking(move || db::get_metadata_by_path(&pool, &db_path))
        .await?
        .ok_or_else(|| AppError::NotFound(format!("No metadata for {path_str}")))?;
    Ok(Json(m))
}

// ----------------------------------------------------------------------
// GET /search?q=...&limit=
// ----------------------------------------------------------------------
#[derive(serde::Deserialize)]
pub struct SearchQuery {
    pub q: String,
    #[serde(default = "default_limit")]
    pub limit: usize,
}

fn default_limit() -> usize {
    50
}

pub async fn search(
    State(state): State<Arc<AppState>>,
    Query(q): Query<SearchQuery>,
) -> AppResult<Json<SearchResponse>> {
    let limit = q.limit.clamp(1, 500);
    let pool = state.pipeline.pool.clone();
    let query = q.q.clone();
    let hits = db::run_blocking(move || db::search_fts(&pool, &query, limit)).await?;
    Ok(Json(SearchResponse {
        query: q.q,
        total: hits.len(),
        hits,
    }))
}

// ----------------------------------------------------------------------
// GET /browse?limit=
// ----------------------------------------------------------------------
#[derive(serde::Deserialize)]
pub struct BrowseQuery {
    #[serde(default = "default_browse")]
    pub limit: usize,
}
fn default_browse() -> usize {
    60
}

pub async fn browse(
    State(state): State<Arc<AppState>>,
    Query(q): Query<BrowseQuery>,
) -> AppResult<Json<BrowseResponse>> {
    let limit = q.limit.clamp(1, 500);
    let pool = state.pipeline.pool.clone();
    let (hits, total) = db::run_blocking(move || {
        let hits = db::recent_processed(&pool, limit)?;
        let total = db::processed_count(&pool)?;
        Ok((hits, total))
    })
    .await?;
    Ok(Json(BrowseResponse { total, hits }))
}

// ----------------------------------------------------------------------
// GET /stats
// ----------------------------------------------------------------------
pub async fn stats(State(state): State<Arc<AppState>>) -> AppResult<Json<StatsResponse>> {
    let pool = state.pipeline.pool.clone();
    Ok(Json(db::run_blocking(move || db::stats(&pool)).await?))
}

// ----------------------------------------------------------------------
// GET /thumb?path=...
// ----------------------------------------------------------------------
const THUMB_EDGE: u32 = 256;

pub async fn thumb(
    State(state): State<Arc<AppState>>,
    Query(q): Query<PathQuery>,
) -> AppResult<Response> {
    let canonical =
        validate_root_async(Arc::clone(&state.pipeline.cfg), PathBuf::from(&q.path)).await?;

    let metadata = tokio::fs::metadata(&canonical).await?;
    if metadata.len() > state.pipeline.cfg.max_file_bytes {
        return Err(AppError::PayloadTooLarge(format!(
            "file size {} exceeds MAX_FILE_BYTES={}",
            metadata.len(),
            state.pipeline.cfg.max_file_bytes
        )));
    }
    let bytes = tokio::fs::read(&canonical).await?;
    let max_pixels = state.pipeline.cfg.max_image_pixels;
    let jpeg = tokio::task::spawn_blocking(move || render_thumb(&bytes, THUMB_EDGE, max_pixels))
        .await
        .map_err(|e| AppError::Internal(format!("thumb join error: {e}")))?
        .map_err(AppError::Llm)?;

    Ok((
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, "image/jpeg"),
            (header::CACHE_CONTROL, "public, max-age=86400"),
        ],
        jpeg,
    )
        .into_response())
}

async fn validate_root_async(cfg: Arc<Config>, candidate: PathBuf) -> AppResult<PathBuf> {
    tokio::task::spawn_blocking(move || validate_root(&cfg, &candidate))
        .await
        .map_err(|e| AppError::Internal(format!("path validation join error: {e}")))?
}

fn finish_job(job: &crate::jobs::JobState) {
    if job.failed_count() > 0 {
        job.finish(
            JobStatus::CompletedWithFailures,
            Some(format!("{} files failed", job.failed_count())),
        );
    } else {
        job.finish(JobStatus::Completed, None);
    }
}

fn render_thumb(bytes: &[u8], edge: u32, max_pixels: u64) -> Result<Vec<u8>, String> {
    let img = crate::transcode::decode_supported_image(bytes, max_pixels)?;
    let (w, h) = (img.width(), img.height());
    let resized = if w.max(h) > edge {
        let scale = edge as f32 / w.max(h) as f32;
        let nw = (w as f32 * scale).round().max(1.0) as u32;
        let nh = (h as f32 * scale).round().max(1.0) as u32;
        img.resize(nw, nh, image::imageops::FilterType::Triangle)
    } else {
        img
    };
    let mut out = Vec::with_capacity(64 * 1024);
    let rgb = resized.to_rgb8();
    let mut enc = image::codecs::jpeg::JpegEncoder::new_with_quality(&mut out, 80);
    enc.encode(
        rgb.as_raw(),
        rgb.width(),
        rgb.height(),
        image::ExtendedColorType::Rgb8,
    )
    .map_err(|e| format!("encode: {e}"))?;
    Ok(out)
}
