mod auth;
mod config;
mod error;
mod models;
mod openai;
mod routes;
mod runs;

use std::sync::atomic::AtomicBool;
use std::sync::Arc;
use std::time::Duration;

use tokio::sync::RwLock;
use tokio::time::MissedTickBehavior;
use tower_http::trace::TraceLayer;

use crate::config::Config;
use crate::openai::OpenAiClient;
use crate::routes::{refresh_models, AppState, ModelCache};
use crate::runs::RunRegistry;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,routing_workbench=info".into()),
        )
        .init();

    let cfg = Config::from_env().expect("Invalid configuration");
    tracing::info!(
        listen = %cfg.listen_addr,
        openai_base_url = %cfg.openai_base_url,
        default_model = %cfg.default_model,
        auth_required = cfg.require_auth,
        "Starting routing-workbench"
    );

    let openai = OpenAiClient::new(
        cfg.openai_base_url.clone(),
        Duration::from_secs(cfg.request_timeout_secs),
    )
    .expect("Failed to build OpenAI client");

    let listen_addr = cfg.listen_addr;
    let state = Arc::new(AppState {
        cfg: Arc::new(cfg),
        openai,
        runs: RunRegistry::default(),
        gateway_reachable: Arc::new(AtomicBool::new(false)),
        models: Arc::new(RwLock::new(ModelCache::default())),
    });

    {
        let state = Arc::clone(&state);
        tokio::spawn(async move {
            let mut ticker = tokio::time::interval(Duration::from_secs(15));
            ticker.set_missed_tick_behavior(MissedTickBehavior::Delay);
            loop {
                let ok = refresh_models(&state).await.is_ok();
                state
                    .gateway_reachable
                    .store(ok, std::sync::atomic::Ordering::Relaxed);
                ticker.tick().await;
            }
        });
    }

    let app = routes::router(Arc::clone(&state)).layer(TraceLayer::new_for_http());
    let listener = tokio::net::TcpListener::bind(listen_addr)
        .await
        .expect("Failed to bind listener");

    tracing::info!("Listening on {}", listen_addr);
    axum::serve(listener, app).await.expect("Server error");
}
