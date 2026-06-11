mod config;
mod db;
mod error;
mod jobs;
mod llm;
mod models;
mod pipeline;
mod routes;
mod transcode;
mod walker;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use axum::middleware;
use axum::response::Html;
use axum::routing::{get, post};
use axum::Router;
use tokio::sync::Semaphore;
use tokio::time::{sleep, MissedTickBehavior};
use tower_http::trace::TraceLayer;

use crate::config::Config;
use crate::jobs::JobRegistry;
use crate::llm::LlmClient;
use crate::pipeline::Pipeline;
use crate::routes::AppState;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,photo_tagger=info".into()),
        )
        .init();

    let cfg = Config::from_env().expect("Invalid configuration");
    tracing::info!(
        listen = %cfg.listen_addr,
        endpoint = %cfg.llm_endpoint,
        model = %cfg.model_name,
        database = "postgres",
        auth_required = cfg.require_auth,
        "Starting photo-tagger"
    );

    let pool = open_pool_with_retry(cfg.database_url.clone()).await;

    let llm = LlmClient::new(
        cfg.llm_endpoint.clone(),
        cfg.model_name.clone(),
        Duration::from_secs(cfg.llm_timeout_secs),
        cfg.llm_max_retries,
    )
    .expect("Failed to build LLM client");

    let cfg_arc = Arc::new(cfg.clone());
    let pipeline = Pipeline {
        cfg: Arc::clone(&cfg_arc),
        pool: pool.clone(),
        llm: llm.clone(),
        hash_sem: Arc::new(Semaphore::new(cfg.max_concurrent_hash)),
        llm_sem: Arc::new(Semaphore::new(cfg.max_concurrent_llm)),
    };

    let database_ready = Arc::new(AtomicBool::new(true));
    let endpoint_reachable = Arc::new(AtomicBool::new(false));
    {
        let flag = Arc::clone(&database_ready);
        let pool = pool.clone();
        tokio::spawn(async move {
            let mut ticker = tokio::time::interval(Duration::from_secs(15));
            ticker.set_missed_tick_behavior(MissedTickBehavior::Delay);
            loop {
                ticker.tick().await;
                let pool = pool.clone();
                let ok = db::run_blocking(move || db::ping(&pool)).await.is_ok();
                flag.store(ok, Ordering::Relaxed);
            }
        });
    }
    {
        let flag = Arc::clone(&endpoint_reachable);
        tokio::spawn(async move {
            let mut ticker = tokio::time::interval(Duration::from_secs(15));
            ticker.set_missed_tick_behavior(MissedTickBehavior::Delay);
            loop {
                ticker.tick().await;
                let ok = llm.ping().await;
                flag.store(ok, Ordering::Relaxed);
            }
        });
    }

    let state = Arc::new(AppState {
        pipeline,
        jobs: Arc::new(JobRegistry::new()),
        database_ready,
        endpoint_reachable,
    });

    let protected = Router::new()
        .route("/", get(index_page))
        .route("/scan", post(routes::start_scan))
        .route("/reprocess", post(routes::reprocess))
        .route("/jobs", get(routes::list_jobs))
        .route("/jobs/{id}", get(routes::get_job))
        .route("/jobs/{id}/events", get(routes::job_events))
        .route("/metadata", get(routes::get_metadata))
        .route("/search", get(routes::search))
        .route("/browse", get(routes::browse))
        .route("/stats", get(routes::stats))
        .route("/thumb", get(routes::thumb))
        .route_layer(middleware::from_fn_with_state(
            Arc::clone(&state),
            routes::require_auth,
        ));

    let app = Router::new()
        .route("/health", get(routes::health))
        .route("/ready", get(routes::ready))
        .merge(protected)
        .layer(TraceLayer::new_for_http())
        .with_state(state);

    let listener = tokio::net::TcpListener::bind(cfg.listen_addr)
        .await
        .expect("Failed to bind listener");

    tracing::info!("Listening on {}", cfg.listen_addr);
    axum::serve(listener, app).await.expect("Server error");
}

async fn index_page() -> Html<&'static str> {
    Html(include_str!("../static/index.html"))
}

async fn open_pool_with_retry(database_url: String) -> db::DbPool {
    const MAX_ATTEMPTS: usize = 60;

    for attempt in 1..=MAX_ATTEMPTS {
        let database_url = database_url.clone();
        match tokio::task::spawn_blocking(move || db::open_pool(&database_url)).await {
            Ok(Ok(pool)) => return pool,
            Ok(Err(err)) if attempt < MAX_ATTEMPTS => {
                tracing::warn!(
                    attempt,
                    max_attempts = MAX_ATTEMPTS,
                    error = %err,
                    "Postgres is not ready; retrying"
                );
            }
            Ok(Err(err)) => {
                panic!("Failed to open Postgres after {MAX_ATTEMPTS} attempts: {err}");
            }
            Err(err) if attempt < MAX_ATTEMPTS => {
                tracing::warn!(
                    attempt,
                    max_attempts = MAX_ATTEMPTS,
                    error = %err,
                    "Postgres initialization task failed; retrying"
                );
            }
            Err(err) => {
                panic!("Postgres initialization task failed after {MAX_ATTEMPTS} attempts: {err}");
            }
        }

        sleep(Duration::from_secs(2)).await;
    }

    unreachable!("retry loop always returns or panics")
}
