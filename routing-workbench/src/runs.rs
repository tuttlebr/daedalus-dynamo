use std::collections::HashMap;
use std::sync::Arc;

use futures::stream::FuturesUnordered;
use futures::StreamExt as _;
use serde_json::{json, Value};
use time::format_description::well_known::Rfc3339;
use time::OffsetDateTime;
use tokio::sync::{broadcast, RwLock};
use uuid::Uuid;

use crate::config::Config;
use crate::error::{AppError, AppResult};
use crate::models::{
    RequestResult, RunConfig, RunCreateRequest, RunDetail, RunEvent, RunMetrics, RunSummary,
};
use crate::openai::{ChatResult, OpenAiClient};

#[derive(Clone, Default)]
pub struct RunRegistry {
    runs: Arc<RwLock<HashMap<String, Arc<Run>>>>,
}

pub struct Run {
    summary: RwLock<RunSummary>,
    request_body: Value,
    results: RwLock<Vec<RequestResult>>,
    events: broadcast::Sender<RunEvent>,
}

impl RunRegistry {
    pub async fn start(
        &self,
        req: RunCreateRequest,
        app_cfg: Arc<Config>,
        openai: OpenAiClient,
    ) -> AppResult<RunDetail> {
        let cfg = normalize_request(req, &app_cfg)?;
        let id = Uuid::new_v4().to_string();
        let (request_body, _) = build_request_body(&cfg, 0);
        let summary = RunSummary {
            id: id.clone(),
            status: "queued".to_string(),
            created_at: now_rfc3339(),
            started_at: None,
            finished_at: None,
            config: cfg.clone(),
            successes: 0,
            errors: 0,
            completed_requests: 0,
            metrics: empty_metrics(),
        };
        let (events, _) = broadcast::channel(256);
        let run = Arc::new(Run {
            summary: RwLock::new(summary),
            request_body,
            results: RwLock::new(Vec::new()),
            events,
        });
        self.runs.write().await.insert(id.clone(), Arc::clone(&run));

        let detail = run.detail().await;
        tokio::spawn(execute_run(id, Arc::clone(&run), cfg, openai));
        Ok(detail)
    }

    pub async fn list(&self) -> Vec<RunSummary> {
        let runs: Vec<Arc<Run>> = self.runs.read().await.values().cloned().collect();
        let mut summaries = Vec::with_capacity(runs.len());
        for run in runs {
            summaries.push(run.summary.read().await.clone());
        }
        summaries.sort_by(|a, b| b.created_at.cmp(&a.created_at));
        summaries
    }

    pub async fn get(&self, id: &str) -> AppResult<Arc<Run>> {
        self.runs
            .read()
            .await
            .get(id)
            .cloned()
            .ok_or_else(|| AppError::NotFound(format!("run not found: {id}")))
    }

    pub async fn get_detail(&self, id: &str) -> AppResult<RunDetail> {
        Ok(self.get(id).await?.detail().await)
    }
}

impl Run {
    pub async fn detail(&self) -> RunDetail {
        RunDetail {
            summary: self.summary.read().await.clone(),
            request_body: self.request_body.clone(),
            results: self.results.read().await.clone(),
        }
    }

    pub fn subscribe(&self) -> broadcast::Receiver<RunEvent> {
        self.events.subscribe()
    }

    async fn emit(
        &self,
        event: &str,
        run_id: &str,
        message: impl Into<String>,
        result: Option<RequestResult>,
    ) {
        let summary = self.summary.read().await.clone();
        let _ = self.events.send(RunEvent {
            event: event.to_string(),
            run_id: run_id.to_string(),
            message: message.into(),
            timestamp: now_rfc3339(),
            summary: Some(summary),
            result,
        });
    }
}

async fn execute_run(id: String, run: Arc<Run>, cfg: RunConfig, openai: OpenAiClient) {
    {
        let mut summary = run.summary.write().await;
        summary.status = "running".to_string();
        summary.started_at = Some(now_rfc3339());
    }
    run.emit("run_started", &id, "run started", None).await;

    let mut tasks = FuturesUnordered::new();
    let mut next_index = 0;
    while next_index < cfg.total_requests && tasks.len() < cfg.concurrency {
        tasks.push(spawn_request(next_index, cfg.clone(), openai.clone()));
        next_index += 1;
    }

    while let Some(joined) = tasks.next().await {
        let result = match joined {
            Ok(result) => result,
            Err(err) => RequestResult {
                index: usize::MAX,
                prompt_group: "unknown".to_string(),
                status: "error".to_string(),
                http_status: None,
                latency_ms: None,
                ttft_ms: None,
                completion_ms: None,
                worker_id: None,
                timing: None,
                output_excerpt: None,
                error: Some(format!("request task failed: {err}")),
            },
        };

        {
            let mut results = run.results.write().await;
            results.push(result.clone());
            results.sort_by_key(|result| result.index);
            let mut summary = run.summary.write().await;
            update_summary_from_results(&mut summary, &results);
        }
        run.emit("request_finished", &id, "request finished", Some(result))
            .await;

        if next_index < cfg.total_requests {
            tasks.push(spawn_request(next_index, cfg.clone(), openai.clone()));
            next_index += 1;
        }
    }

    {
        let results = run.results.read().await;
        let mut summary = run.summary.write().await;
        update_summary_from_results(&mut summary, &results);
        summary.status = if summary.errors == summary.config.total_requests {
            "failed".to_string()
        } else {
            "completed".to_string()
        };
        summary.finished_at = Some(now_rfc3339());
    }
    run.emit("run_finished", &id, "run finished", None).await;
}

fn spawn_request(
    index: usize,
    cfg: RunConfig,
    openai: OpenAiClient,
) -> tokio::task::JoinHandle<RequestResult> {
    tokio::spawn(async move { run_one_request(index, cfg, openai).await })
}

async fn run_one_request(index: usize, cfg: RunConfig, openai: OpenAiClient) -> RequestResult {
    let (body, prompt_group) = build_request_body(&cfg, index);
    match openai.chat_completions_stream(body).await {
        Ok(result) => request_success(index, prompt_group, result),
        Err(err) => RequestResult {
            index,
            prompt_group,
            status: "error".to_string(),
            http_status: None,
            latency_ms: None,
            ttft_ms: None,
            completion_ms: None,
            worker_id: None,
            timing: None,
            output_excerpt: None,
            error: Some(err.to_string()),
        },
    }
}

fn request_success(index: usize, prompt_group: String, result: ChatResult) -> RequestResult {
    RequestResult {
        index,
        prompt_group,
        status: "ok".to_string(),
        http_status: Some(result.http_status),
        latency_ms: Some(result.latency_ms),
        ttft_ms: result.ttft_ms,
        completion_ms: Some(result.completion_ms),
        worker_id: result.worker_id,
        timing: result.timing,
        output_excerpt: result.output_excerpt,
        error: None,
    }
}

fn normalize_request(req: RunCreateRequest, app_cfg: &Config) -> AppResult<RunConfig> {
    let preset = req
        .preset
        .unwrap_or_else(|| "repeated-shared-prefix".to_string())
        .to_ascii_lowercase();
    if !matches!(
        preset.as_str(),
        "cold-prompt" | "repeated-shared-prefix" | "mixed-prefixes" | "priority-hints"
    ) {
        return Err(AppError::BadRequest(format!("unknown preset: {preset}")));
    }

    let total_requests = req.total_requests.unwrap_or(8);
    if total_requests == 0 || total_requests > app_cfg.max_run_requests {
        return Err(AppError::BadRequest(format!(
            "total_requests must be between 1 and {}",
            app_cfg.max_run_requests
        )));
    }

    let concurrency = req.concurrency.unwrap_or(2);
    if concurrency == 0 || concurrency > app_cfg.max_concurrency {
        return Err(AppError::BadRequest(format!(
            "concurrency must be between 1 and {}",
            app_cfg.max_concurrency
        )));
    }

    let max_tokens = req.max_tokens.unwrap_or(96);
    if max_tokens == 0 || max_tokens > 8192 {
        return Err(AppError::BadRequest(
            "max_tokens must be between 1 and 8192".to_string(),
        ));
    }

    let temperature = req.temperature.unwrap_or(0.2);
    if !(0.0..=2.0).contains(&temperature) {
        return Err(AppError::BadRequest(
            "temperature must be between 0.0 and 2.0".to_string(),
        ));
    }

    Ok(RunConfig {
        model: req
            .model
            .filter(|model| !model.trim().is_empty())
            .unwrap_or_else(|| app_cfg.default_model.clone()),
        preset: preset.clone(),
        total_requests,
        concurrency: concurrency.min(total_requests),
        shared_prefix_tokens: req.shared_prefix_tokens.unwrap_or(256).min(4096),
        max_tokens,
        temperature,
        include_nvext: req.include_nvext.unwrap_or(true) || preset == "priority-hints",
    })
}

fn build_request_body(cfg: &RunConfig, index: usize) -> (Value, String) {
    let (prompt, prompt_group) = build_prompt(cfg, index);
    let mut body = json!({
        "model": cfg.model,
        "stream": true,
        "messages": [
            {
                "role": "system",
                "content": "You are answering a Dynamo routing benchmark. Respond with concise plain text only."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "max_tokens": cfg.max_tokens,
        "temperature": cfg.temperature
    });

    if cfg.include_nvext {
        let mut nvext = json!({
            "extra_fields": ["worker_id", "timing"]
        });
        if cfg.preset == "priority-hints" {
            nvext["agent_hints"] = json!({
                "priority": if index % 2 == 0 { 8 } else { 2 },
                "osl": cfg.shared_prefix_tokens + cfg.max_tokens as usize,
                "speculative_prefill": true
            });
        }
        body["nvext"] = nvext;
    }

    (body, prompt_group)
}

fn build_prompt(cfg: &RunConfig, index: usize) -> (String, String) {
    match cfg.preset.as_str() {
        "cold-prompt" => (
            format!(
                "Request {index}: Give a two sentence explanation of model-header routing through a Kubernetes Gateway."
            ),
            "cold".to_string(),
        ),
        "mixed-prefixes" => {
            let group = match index % 3 {
                0 => "prefix-a",
                1 => "prefix-b",
                _ => "prefix-c",
            };
            (
                format!(
                    "{}\n\nRequest {index}: Explain one observable cache-locality effect for this prefix group.",
                    shared_prefix(cfg.shared_prefix_tokens, group)
                ),
                group.to_string(),
            )
        }
        "priority-hints" => (
            format!(
                "{}\n\nRequest {index}: In one paragraph, describe how priority hints can influence scheduling.",
                shared_prefix(cfg.shared_prefix_tokens, "priority")
            ),
            if index % 2 == 0 {
                "priority-high".to_string()
            } else {
                "priority-low".to_string()
            },
        ),
        _ => (
            format!(
                "{}\n\nRequest {index}: In two short bullets, explain why KV-cache locality can reduce latency.",
                shared_prefix(cfg.shared_prefix_tokens, "shared")
            ),
            "shared".to_string(),
        ),
    }
}

fn shared_prefix(tokens: usize, label: &str) -> String {
    (0..tokens)
        .map(|i| format!("{label}_{:04}", i % 97))
        .collect::<Vec<_>>()
        .join(" ")
}

fn update_summary_from_results(summary: &mut RunSummary, results: &[RequestResult]) {
    summary.completed_requests = results.len();
    summary.successes = results
        .iter()
        .filter(|result| result.status == "ok")
        .count();
    summary.errors = results.len().saturating_sub(summary.successes);
    summary.metrics = metrics(results);
}

fn metrics(results: &[RequestResult]) -> RunMetrics {
    let latencies: Vec<u128> = results
        .iter()
        .filter_map(|result| result.latency_ms)
        .collect();
    let ttfts: Vec<u128> = results.iter().filter_map(|result| result.ttft_ms).collect();
    let completions: Vec<u128> = results
        .iter()
        .filter_map(|result| result.completion_ms)
        .collect();
    RunMetrics {
        latency_ms_avg: average(&latencies),
        latency_ms_p50: percentile(latencies.clone(), 50),
        latency_ms_p95: percentile(latencies, 95),
        ttft_ms_avg: average(&ttfts),
        completion_ms_avg: average(&completions),
    }
}

fn average(values: &[u128]) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    Some(values.iter().sum::<u128>() as f64 / values.len() as f64)
}

fn percentile(mut values: Vec<u128>, pct: usize) -> Option<u128> {
    if values.is_empty() {
        return None;
    }
    values.sort_unstable();
    let index = ((values.len() - 1) * pct).div_ceil(100);
    values.get(index).copied()
}

fn empty_metrics() -> RunMetrics {
    RunMetrics {
        latency_ms_avg: None,
        latency_ms_p50: None,
        latency_ms_p95: None,
        ttft_ms_avg: None,
        completion_ms_avg: None,
    }
}

fn now_rfc3339() -> String {
    OffsetDateTime::now_utc()
        .format(&Rfc3339)
        .unwrap_or_else(|_| "1970-01-01T00:00:00Z".to_string())
}

#[cfg(test)]
mod tests {
    use super::{build_request_body, normalize_request};
    use crate::config::Config;
    use crate::models::RunCreateRequest;

    fn cfg() -> Config {
        Config {
            listen_addr: "127.0.0.1:3000".parse().unwrap(),
            openai_base_url: "http://localhost:8000/v1".to_string(),
            default_model: "model-a".to_string(),
            require_auth: false,
            auth_user: "routing-workbench".to_string(),
            auth_token: None,
            request_timeout_secs: 60,
            max_run_requests: 128,
            max_concurrency: 16,
        }
    }

    #[test]
    fn request_body_is_text_only() {
        let run = normalize_request(
            RunCreateRequest {
                model: None,
                preset: Some("repeated-shared-prefix".to_string()),
                total_requests: Some(1),
                concurrency: Some(1),
                shared_prefix_tokens: Some(8),
                max_tokens: Some(16),
                temperature: Some(0.1),
                include_nvext: Some(true),
            },
            &cfg(),
        )
        .unwrap();
        let (body, _) = build_request_body(&run, 0);
        let text = serde_json::to_string(&body).unwrap();
        assert!(text.contains("\"content\":\"shared_"));
        assert!(!text.contains("image_url"));
        assert!(!text.contains("audio"));
        assert!(!text.contains("video"));
    }

    #[test]
    fn priority_preset_forces_nvext() {
        let run = normalize_request(
            RunCreateRequest {
                model: None,
                preset: Some("priority-hints".to_string()),
                total_requests: Some(1),
                concurrency: Some(1),
                shared_prefix_tokens: None,
                max_tokens: None,
                temperature: None,
                include_nvext: Some(false),
            },
            &cfg(),
        )
        .unwrap();
        let (body, _) = build_request_body(&run, 0);
        assert!(body.get("nvext").is_some());
        assert!(body["nvext"].get("agent_hints").is_some());
    }
}
