use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Serialize)]
pub struct HealthResponse {
    pub status: &'static str,
}

#[derive(Debug, Clone, Serialize)]
pub struct ReadyResponse {
    pub status: &'static str,
    pub gateway_reachable: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct StatusResponse {
    pub gateway_reachable: bool,
    pub selected_model: String,
    pub discovered_models: Vec<String>,
    pub cached: bool,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct RunCreateRequest {
    pub model: Option<String>,
    pub preset: Option<String>,
    pub total_requests: Option<usize>,
    pub concurrency: Option<usize>,
    pub shared_prefix_tokens: Option<usize>,
    pub max_tokens: Option<u32>,
    pub temperature: Option<f32>,
    pub include_nvext: Option<bool>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RunConfig {
    pub model: String,
    pub preset: String,
    pub total_requests: usize,
    pub concurrency: usize,
    pub shared_prefix_tokens: usize,
    pub max_tokens: u32,
    pub temperature: f32,
    pub include_nvext: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RunMetrics {
    pub latency_ms_avg: Option<f64>,
    pub latency_ms_p50: Option<u128>,
    pub latency_ms_p95: Option<u128>,
    pub ttft_ms_avg: Option<f64>,
    pub completion_ms_avg: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RunSummary {
    pub id: String,
    pub status: String,
    pub created_at: String,
    pub started_at: Option<String>,
    pub finished_at: Option<String>,
    pub config: RunConfig,
    pub successes: usize,
    pub errors: usize,
    pub completed_requests: usize,
    pub metrics: RunMetrics,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RequestResult {
    pub index: usize,
    pub prompt_group: String,
    pub status: String,
    pub http_status: Option<u16>,
    pub latency_ms: Option<u128>,
    pub ttft_ms: Option<u128>,
    pub completion_ms: Option<u128>,
    pub worker_id: Option<String>,
    pub timing: Option<Value>,
    pub output_excerpt: Option<String>,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RunDetail {
    pub summary: RunSummary,
    pub request_body: Value,
    pub results: Vec<RequestResult>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RunEvent {
    pub event: String,
    pub run_id: String,
    pub message: String,
    pub timestamp: String,
    pub summary: Option<RunSummary>,
    pub result: Option<RequestResult>,
}
