use std::convert::Infallible;
use std::sync::atomic::Ordering;
use std::sync::Arc;

use async_stream::stream;
use axum::extract::{Path, State};
use axum::middleware;
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::Html;
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::Value;
use tokio::sync::RwLock;
use tokio_stream::Stream;

use crate::auth;
use crate::config::Config;
use crate::error::{AppError, AppResult};
use crate::models::{HealthResponse, ReadyResponse, RunCreateRequest, StatusResponse};
use crate::openai::{extract_model_ids, OpenAiClient};
use crate::runs::{Run, RunRegistry};

#[derive(Default)]
pub struct ModelCache {
    pub value: Option<Value>,
}

pub struct AppState {
    pub cfg: Arc<Config>,
    pub openai: OpenAiClient,
    pub runs: RunRegistry,
    pub gateway_reachable: Arc<std::sync::atomic::AtomicBool>,
    pub models: Arc<RwLock<ModelCache>>,
}

pub fn router(state: Arc<AppState>) -> Router {
    let protected = Router::new()
        .route("/", get(index_page))
        .route("/api/status", get(status))
        .route("/api/models", get(models))
        .route("/api/runs", post(start_run).get(list_runs))
        .route("/api/runs/{id}", get(get_run))
        .route("/api/runs/{id}/events", get(run_events))
        .route_layer(middleware::from_fn_with_state(
            Arc::clone(&state),
            auth::require_auth,
        ));

    Router::new()
        .route("/health", get(health))
        .route("/ready", get(ready))
        .merge(protected)
        .with_state(state)
}

pub async fn refresh_models(state: &Arc<AppState>) -> AppResult<Value> {
    let value = state.openai.fetch_models().await?;
    state.models.write().await.value = Some(value.clone());
    Ok(value)
}

async fn index_page() -> Html<&'static str> {
    Html(include_str!("../static/index.html"))
}

async fn health() -> Json<HealthResponse> {
    Json(HealthResponse { status: "ok" })
}

async fn ready(
    State(state): State<Arc<AppState>>,
) -> (axum::http::StatusCode, Json<ReadyResponse>) {
    let gateway_reachable = state.gateway_reachable.load(Ordering::Relaxed);
    let ready = gateway_reachable;
    let code = if ready {
        axum::http::StatusCode::OK
    } else {
        axum::http::StatusCode::SERVICE_UNAVAILABLE
    };
    (
        code,
        Json(ReadyResponse {
            status: if ready { "ready" } else { "not_ready" },
            gateway_reachable,
        }),
    )
}

async fn status(State(state): State<Arc<AppState>>) -> Json<StatusResponse> {
    match refresh_models(&state).await {
        Ok(value) => {
            state.gateway_reachable.store(true, Ordering::Relaxed);
            Json(StatusResponse {
                gateway_reachable: true,
                selected_model: state.cfg.default_model.clone(),
                discovered_models: extract_model_ids(&value),
                cached: false,
                error: None,
            })
        }
        Err(err) => {
            state.gateway_reachable.store(false, Ordering::Relaxed);
            let cached = state.models.read().await.value.clone();
            Json(StatusResponse {
                gateway_reachable: false,
                selected_model: state.cfg.default_model.clone(),
                discovered_models: cached.as_ref().map(extract_model_ids).unwrap_or_default(),
                cached: cached.is_some(),
                error: Some(err.to_string()),
            })
        }
    }
}

async fn models(State(state): State<Arc<AppState>>) -> AppResult<Json<Value>> {
    match refresh_models(&state).await {
        Ok(value) => {
            state.gateway_reachable.store(true, Ordering::Relaxed);
            Ok(Json(value))
        }
        Err(err) => {
            state.gateway_reachable.store(false, Ordering::Relaxed);
            if let Some(value) = state.models.read().await.value.clone() {
                Ok(Json(value))
            } else {
                Err(err)
            }
        }
    }
}

async fn start_run(
    State(state): State<Arc<AppState>>,
    Json(req): Json<RunCreateRequest>,
) -> AppResult<(axum::http::StatusCode, Json<crate::models::RunDetail>)> {
    let detail = state
        .runs
        .start(req, Arc::clone(&state.cfg), state.openai.clone())
        .await?;
    Ok((axum::http::StatusCode::ACCEPTED, Json(detail)))
}

async fn list_runs(State(state): State<Arc<AppState>>) -> Json<Vec<crate::models::RunSummary>> {
    Json(state.runs.list().await)
}

async fn get_run(
    State(state): State<Arc<AppState>>,
    Path(id): Path<String>,
) -> AppResult<Json<crate::models::RunDetail>> {
    Ok(Json(state.runs.get_detail(&id).await?))
}

async fn run_events(
    State(state): State<Arc<AppState>>,
    Path(id): Path<String>,
) -> AppResult<Sse<impl Stream<Item = Result<Event, Infallible>>>> {
    let run = state.runs.get(&id).await?;
    Ok(run_event_stream(run).await)
}

async fn run_event_stream(run: Arc<Run>) -> Sse<impl Stream<Item = Result<Event, Infallible>>> {
    let snapshot = run.detail().await;
    let mut rx = run.subscribe();
    let stream = stream! {
        let initial = Event::default()
            .event("snapshot")
            .json_data(snapshot)
            .unwrap_or_else(|_| Event::default().event("error").data("snapshot serialization failed"));
        yield Ok(initial);

        loop {
            match rx.recv().await {
                Ok(event) => {
                    let sse = Event::default()
                        .event(event.event.clone())
                        .json_data(event)
                        .unwrap_or_else(|_| Event::default().event("error").data("event serialization failed"));
                    yield Ok(sse);
                }
                Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => continue,
                Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
            }
        }
    };

    Sse::new(stream).keep_alive(KeepAlive::default())
}

impl From<&str> for AppError {
    fn from(value: &str) -> Self {
        AppError::Internal(value.to_string())
    }
}
