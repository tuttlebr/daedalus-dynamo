use std::time::{Duration, Instant};

use futures::StreamExt as _;
use reqwest::Client;
use serde_json::Value;

use crate::error::{AppError, AppResult};

#[derive(Clone)]
pub struct OpenAiClient {
    http: Client,
    base_url: String,
}

#[derive(Debug, Clone)]
pub struct ChatResult {
    pub http_status: u16,
    pub latency_ms: u128,
    pub ttft_ms: Option<u128>,
    pub completion_ms: u128,
    pub worker_id: Option<String>,
    pub timing: Option<Value>,
    pub output_excerpt: Option<String>,
}

impl OpenAiClient {
    pub fn new(base_url: String, timeout: Duration) -> AppResult<Self> {
        let http = Client::builder()
            .timeout(timeout)
            .user_agent(concat!("routing-workbench/", env!("CARGO_PKG_VERSION")))
            .build()
            .map_err(AppError::Http)?;
        Ok(OpenAiClient { http, base_url })
    }

    pub async fn fetch_models(&self) -> AppResult<Value> {
        let resp = self
            .http
            .get(format!("{}/models", self.base_url))
            .send()
            .await?;
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        if !status.is_success() {
            return Err(AppError::Gateway(format!(
                "GET /v1/models returned HTTP {status}: {}",
                excerpt(&body, 500)
            )));
        }
        serde_json::from_str(&body).map_err(AppError::Json)
    }

    pub async fn chat_completions_stream(&self, body: Value) -> AppResult<ChatResult> {
        let start = Instant::now();
        let resp = self
            .http
            .post(format!("{}/chat/completions", self.base_url))
            .json(&body)
            .send()
            .await?;

        let status = resp.status();
        let status_code = status.as_u16();
        if !status.is_success() {
            let body = resp.text().await.unwrap_or_default();
            return Err(AppError::Gateway(format!(
                "POST /v1/chat/completions returned HTTP {status}: {}",
                excerpt(&body, 500)
            )));
        }

        let mut stream = resp.bytes_stream();
        let mut buffer = String::new();
        let mut raw = String::new();
        let mut saw_sse = false;
        let mut ttft_ms = None;
        let mut output = String::new();
        let mut worker_id = None;
        let mut timing = None;

        while let Some(chunk) = stream.next().await {
            let chunk = chunk?;
            let text = String::from_utf8_lossy(&chunk);
            raw.push_str(&text);
            buffer.push_str(&text);

            while let Some(pos) = buffer.find('\n') {
                let line = buffer[..pos].trim_end_matches('\r').to_string();
                buffer = buffer[pos + 1..].to_string();
                if process_sse_line(
                    &line,
                    start,
                    &mut saw_sse,
                    &mut ttft_ms,
                    &mut output,
                    &mut worker_id,
                    &mut timing,
                )? {
                    break;
                }
            }
        }

        if !buffer.trim().is_empty() {
            process_sse_line(
                buffer.trim_end_matches('\r'),
                start,
                &mut saw_sse,
                &mut ttft_ms,
                &mut output,
                &mut worker_id,
                &mut timing,
            )?;
        }

        if !saw_sse && !raw.trim().is_empty() {
            let value: Value = serde_json::from_str(&raw)?;
            output.push_str(&extract_message_text(&value).unwrap_or_default());
            worker_id = worker_id.or_else(|| find_string_key(&value, "worker_id"));
            timing = timing.or_else(|| find_key(&value, "timing").cloned());
        }

        let elapsed = start.elapsed().as_millis();
        Ok(ChatResult {
            http_status: status_code,
            latency_ms: elapsed,
            ttft_ms,
            completion_ms: elapsed,
            worker_id,
            timing,
            output_excerpt: nonempty_excerpt(&output, 240),
        })
    }
}

fn process_sse_line(
    line: &str,
    start: Instant,
    saw_sse: &mut bool,
    ttft_ms: &mut Option<u128>,
    output: &mut String,
    worker_id: &mut Option<String>,
    timing: &mut Option<Value>,
) -> AppResult<bool> {
    let Some(data) = line.strip_prefix("data:") else {
        return Ok(false);
    };
    let data = data.trim();
    if data.is_empty() {
        return Ok(false);
    }
    *saw_sse = true;
    if data == "[DONE]" {
        return Ok(true);
    }
    if ttft_ms.is_none() {
        *ttft_ms = Some(start.elapsed().as_millis());
    }

    let value: Value = serde_json::from_str(data)?;
    if let Some(text) = extract_delta_text(&value) {
        output.push_str(&text);
    }
    if worker_id.is_none() {
        *worker_id = find_string_key(&value, "worker_id");
    }
    if timing.is_none() {
        *timing = find_key(&value, "timing").cloned();
    }
    Ok(false)
}

fn extract_delta_text(value: &Value) -> Option<String> {
    let choice = value.get("choices")?.as_array()?.first()?;
    let delta = choice.get("delta").or_else(|| choice.get("message"))?;
    for key in ["content", "reasoning_content"] {
        if let Some(text) = delta.get(key).and_then(Value::as_str) {
            if !text.is_empty() {
                return Some(text.to_string());
            }
        }
    }
    None
}

fn extract_message_text(value: &Value) -> Option<String> {
    let choice = value.get("choices")?.as_array()?.first()?;
    let message = choice.get("message")?;
    for key in ["content", "reasoning_content"] {
        if let Some(text) = message.get(key).and_then(Value::as_str) {
            if !text.is_empty() {
                return Some(text.to_string());
            }
        }
    }
    None
}

pub fn extract_model_ids(value: &Value) -> Vec<String> {
    value
        .get("data")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.get("id").and_then(Value::as_str))
                .map(ToOwned::to_owned)
                .collect()
        })
        .unwrap_or_default()
}

fn find_string_key(value: &Value, key: &str) -> Option<String> {
    find_key(value, key).and_then(|value| match value {
        Value::String(text) => Some(text.clone()),
        Value::Number(number) => Some(number.to_string()),
        _ => None,
    })
}

fn find_key<'a>(value: &'a Value, key: &str) -> Option<&'a Value> {
    match value {
        Value::Object(map) => {
            if let Some(found) = map.get(key) {
                return Some(found);
            }
            map.values().find_map(|child| find_key(child, key))
        }
        Value::Array(items) => items.iter().find_map(|child| find_key(child, key)),
        _ => None,
    }
}

fn nonempty_excerpt(value: &str, max: usize) -> Option<String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(excerpt(trimmed, max))
    }
}

fn excerpt(value: &str, max: usize) -> String {
    let mut out = String::new();
    for ch in value.chars().take(max) {
        out.push(ch);
    }
    if value.chars().count() > max {
        out.push_str("...");
    }
    out
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{extract_model_ids, find_string_key};

    #[test]
    fn extracts_model_ids_from_openai_list() {
        let value = json!({"object":"list","data":[{"id":"a"},{"id":"b"}]});
        assert_eq!(extract_model_ids(&value), vec!["a", "b"]);
    }

    #[test]
    fn finds_nested_worker_id() {
        let value = json!({"choices":[],"nvext":{"extra_fields":{"worker_id":"worker-1"}}});
        assert_eq!(
            find_string_key(&value, "worker_id").as_deref(),
            Some("worker-1")
        );
    }
}
