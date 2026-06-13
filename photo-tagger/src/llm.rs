use std::sync::Arc;
use std::time::Duration;

use base64::Engine;
use regex::Regex;
use reqwest::Client;
use serde::Deserialize;
use serde_json::json;

use crate::config::ImageDelivery;
use crate::error::AppError;
use crate::images::ImageStore;
use crate::models::{PhotoContent, PROMPT_VERSION};
use crate::transcode::PreparedImage;

pub const SYSTEM_PROMPT: &str = r#"You are an image cataloging assistant. Analyze the supplied image and emit ONE JSON object that exactly matches the schema below. Do not include markdown fences, prose, or <think> tags in your final answer — only the JSON object.

Schema (all fields required; use empty arrays / "" when nothing applies):
{
  "caption": string (≤200 chars, single sentence),
  "description": string (2–4 sentences),
  "tags": string[] (5–25 lowercase tokens, single or hyphenated words),
  "scene": string,
  "setting_type": "indoor"|"outdoor"|"studio"|"underwater"|"aerial"|"mixed"|"unknown",
  "detected_text": string[] (verbatim text visible in the image; empty if none),
  "dominant_colors": string[] (3–6 entries, CSS color names or #rrggbb),
  "people": [{ "count_estimate": int, "apparent_age_range": "child"|"teen"|"adult"|"senior"|"mixed"|null, "activity": string|null }],
  "objects": string[],
  "quality": { "sharpness": "sharp"|"slightly_soft"|"blurry",
               "exposure": "balanced"|"underexposed"|"overexposed",
               "composition": string,
               "overall_score": int (1–10) }
}

Rules:
- Be conservative; do not invent text that isn't visible.
- Tags should aid future text search: include subject, setting, mood, notable objects.
- Output JSON only.
"#;

const USER_TEXT: &str = "Describe this image and return the JSON object.";
const RETRY_USER_TEXT: &str =
    "Your previous response was not valid JSON. Output ONLY a single JSON object that matches the schema. No prose, no markdown, no <think> tags.";

#[derive(Debug, Clone)]
pub struct LlmClient {
    http: Client,
    endpoint: String,
    model: String,
    max_retries: u32,
    delivery: ImageDelivery,
    image_base_url: String,
    images: Arc<ImageStore>,
}

#[derive(Debug, Clone)]
pub struct LlmOutput {
    pub content: PhotoContent,
    pub raw: String,
    pub model_version: Option<String>,
}

impl LlmClient {
    pub fn new(
        endpoint: String,
        model: String,
        timeout: Duration,
        max_retries: u32,
        delivery: ImageDelivery,
        image_base_url: String,
        images: Arc<ImageStore>,
    ) -> Result<Self, AppError> {
        let http = Client::builder()
            .timeout(timeout)
            .user_agent(concat!("photo-tagger/", env!("CARGO_PKG_VERSION")))
            .build()
            .map_err(AppError::Http)?;
        Ok(LlmClient {
            http,
            endpoint,
            model,
            max_retries,
            delivery,
            image_base_url,
            images,
        })
    }

    pub async fn ping(&self) -> bool {
        // Cheap reachability check: tiny text-only request, short timeout.
        let body = json!({
            "model": self.model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "ok"}]
        });
        match self
            .http
            .post(&self.endpoint)
            .timeout(Duration::from_secs(5))
            .json(&body)
            .send()
            .await
        {
            Ok(r) => r.status().is_success(),
            Err(_) => false,
        }
    }

    pub async fn analyze(&self, prepared: &PreparedImage) -> Result<LlmOutput, AppError> {
        // In `url` mode, register the prepared bytes and hand the worker a
        // short-lived HTTP URL for backends that cannot consume data URLs.
        // `_image_handle` keeps the entry alive across retries and removes it
        // from the store when this call returns.
        let mut _image_handle = None;
        let image_url = match self.delivery {
            ImageDelivery::DataUrl => build_data_url(prepared),
            ImageDelivery::Url => {
                let handle = self
                    .images
                    .insert(prepared.bytes.clone(), prepared.content_type.mime());
                let url = format!("{}/llm-image/{}", self.image_base_url, handle.token());
                _image_handle = Some(handle);
                url
            }
        };

        // Initial attempt — strict JSON instruction, low temperature.
        let mut last_err: Option<String> = None;
        let mut last_raw: Option<String> = None;

        for attempt in 0..=self.max_retries {
            // First attempt: strict prompt; fallback attempts: stronger nudge + temp 0.
            let temperature = if attempt == 0 { 0.2 } else { 0.0 };
            let user_text = if attempt == 0 {
                USER_TEXT
            } else {
                RETRY_USER_TEXT
            };

            let req_body = json!({
                "model": self.model,
                "max_tokens": 2048,
                "temperature": temperature,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {"type": "text", "text": user_text}
                    ]}
                ]
            });

            let resp = match self.http.post(&self.endpoint).json(&req_body).send().await {
                Ok(r) => r,
                Err(e) => {
                    last_err = Some(format!("HTTP error: {e}"));
                    if attempt < self.max_retries {
                        backoff(attempt).await;
                        continue;
                    }
                    break;
                }
            };

            let status = resp.status();
            if !status.is_success() {
                let body = resp.text().await.unwrap_or_default();
                last_err = Some(format!("HTTP {status}: {body}"));
                // Retry on 429/5xx
                if (status.as_u16() == 429 || status.is_server_error())
                    && attempt < self.max_retries
                {
                    backoff(attempt).await;
                    continue;
                }
                break;
            }

            let resp_text = match resp.text().await {
                Ok(s) => s,
                Err(e) => {
                    last_err = Some(format!("Response read error: {e}"));
                    break;
                }
            };
            let raw_body: ChatCompletionResponse = match serde_json::from_str(&resp_text) {
                Ok(v) => v,
                Err(e) => {
                    last_err = Some(format!(
                        "Response decode error: {e}; body excerpt: {}",
                        excerpt(&resp_text, 200)
                    ));
                    break;
                }
            };

            // Some reasoning parsers route the structured-JSON
            // answer into `reasoning_content` instead of `content`. Prefer
            // `content` when it has anything; fall back to `reasoning_content`.
            let content_str = raw_body
                .choices
                .first()
                .and_then(|c| {
                    c.message
                        .content
                        .as_ref()
                        .filter(|s| !s.trim().is_empty())
                        .cloned()
                        .or_else(|| c.message.reasoning_content.clone())
                })
                .unwrap_or_default();
            last_raw = Some(content_str.clone());

            let cleaned = clean_response(&content_str);

            match parse_photo_content(&cleaned) {
                Ok(content) => {
                    return Ok(LlmOutput {
                        content,
                        raw: content_str,
                        model_version: raw_body.model.or_else(|| Some(self.model.clone())),
                    });
                }
                Err(e) => {
                    last_err = Some(format!("JSON parse error: {e}"));
                    // try a relaxed extraction by snipping the outermost JSON object
                    if let Some(snip) = extract_outer_json(&cleaned) {
                        if let Ok(content) = parse_photo_content(&snip) {
                            return Ok(LlmOutput {
                                content,
                                raw: content_str,
                                model_version: raw_body.model.or_else(|| Some(self.model.clone())),
                            });
                        }
                    }
                    if attempt < self.max_retries {
                        backoff(attempt).await;
                        continue;
                    }
                    break;
                }
            }
        }

        let msg = last_err.unwrap_or_else(|| "Unknown LLM failure".to_string());
        if let Some(raw) = last_raw {
            tracing::warn!(error = %msg, raw_excerpt = %excerpt(&raw, 400), "LLM call failed");
        } else {
            tracing::warn!(error = %msg, "LLM call failed");
        }
        Err(AppError::Llm(msg))
    }
}

pub fn prompt_version() -> &'static str {
    PROMPT_VERSION
}

fn build_data_url(p: &PreparedImage) -> String {
    let b64 = base64::engine::general_purpose::STANDARD.encode(&p.bytes);
    format!("data:{};base64,{}", p.content_type.mime(), b64)
}

fn parse_photo_content(s: &str) -> Result<PhotoContent, String> {
    let content = serde_json::from_str::<PhotoContent>(s).map_err(|e| e.to_string())?;
    content.validate()?;
    Ok(content)
}

fn clean_response(s: &str) -> String {
    // Strip leading <think>...</think> blocks (greedy across multiline).
    let think_re = Regex::new(r"(?s)^\s*<think>.*?</think>\s*").unwrap();
    let stripped = think_re.replace(s, "").to_string();

    // Remove markdown fences if present.
    let fence_re = Regex::new(r"(?s)^\s*```(?:json)?\s*(.+?)\s*```\s*$").unwrap();
    if let Some(c) = fence_re.captures(&stripped) {
        return c
            .get(1)
            .map(|m| m.as_str().trim().to_string())
            .unwrap_or(stripped);
    }
    stripped.trim().to_string()
}

/// Extract the outermost balanced JSON object from a string, if any.
fn extract_outer_json(s: &str) -> Option<String> {
    let bytes = s.as_bytes();
    let start = bytes.iter().position(|&b| b == b'{')?;
    let mut depth = 0i32;
    let mut in_str = false;
    let mut esc = false;
    for (i, &b) in bytes.iter().enumerate().skip(start) {
        if in_str {
            if esc {
                esc = false;
            } else if b == b'\\' {
                esc = true;
            } else if b == b'"' {
                in_str = false;
            }
            continue;
        }
        match b {
            b'"' => in_str = true,
            b'{' => depth += 1,
            b'}' => {
                depth -= 1;
                if depth == 0 {
                    return Some(s[start..=i].to_string());
                }
            }
            _ => {}
        }
    }
    None
}

fn excerpt(s: &str, max: usize) -> String {
    if s.len() <= max {
        s.to_string()
    } else {
        let end = s
            .char_indices()
            .map(|(i, _)| i)
            .take_while(|i| *i <= max)
            .last()
            .unwrap_or(0);
        format!("{}...", &s[..end])
    }
}

async fn backoff(attempt: u32) {
    let delay_ms = match attempt {
        0 => 200,
        1 => 1000,
        _ => 5000,
    };
    tokio::time::sleep(Duration::from_millis(delay_ms)).await;
}

#[derive(Debug, Deserialize)]
struct ChatCompletionResponse {
    #[serde(default)]
    choices: Vec<Choice>,
    #[serde(default)]
    model: Option<String>,
}

#[derive(Debug, Deserialize)]
struct Choice {
    message: Message,
}

#[derive(Debug, Deserialize)]
struct Message {
    #[serde(default)]
    content: Option<String>,
    #[serde(default)]
    reasoning_content: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strips_think_block() {
        let raw = "<think>I should reason about this</think>\n{\"caption\":\"x\"}";
        let cleaned = clean_response(raw);
        assert_eq!(cleaned, "{\"caption\":\"x\"}");
    }

    #[test]
    fn strips_markdown_fence() {
        let raw = "```json\n{\"caption\":\"x\"}\n```";
        let cleaned = clean_response(raw);
        assert_eq!(cleaned, "{\"caption\":\"x\"}");
    }

    #[test]
    fn extract_outer_json_basic() {
        let s = "garbage {\"a\":1,\"b\":{\"c\":2}} trailing";
        assert_eq!(
            extract_outer_json(s),
            Some("{\"a\":1,\"b\":{\"c\":2}}".to_string())
        );
    }

    #[test]
    fn extract_outer_json_with_strings() {
        let s = "{\"k\":\"} not the end\"}";
        assert_eq!(extract_outer_json(s), Some(s.to_string()));
    }

    #[test]
    fn parses_response_with_content() {
        let body =
            r#"{"choices":[{"message":{"content":"{\"caption\":\"x\"}","role":"assistant"}}]}"#;
        let r: ChatCompletionResponse = serde_json::from_str(body).unwrap();
        let msg = &r.choices[0].message;
        assert_eq!(msg.content.as_deref(), Some("{\"caption\":\"x\"}"));
        assert_eq!(msg.reasoning_content, None);
    }

    #[test]
    fn parses_response_with_null_content_and_reasoning() {
        // Mirrors what reasoning parsers can return.
        let body = r#"{"choices":[{"message":{"content":null,"role":"assistant","reasoning_content":"{\"caption\":\"x\"}"}}]}"#;
        let r: ChatCompletionResponse = serde_json::from_str(body).unwrap();
        let msg = &r.choices[0].message;
        assert!(msg.content.is_none());
        assert_eq!(
            msg.reasoning_content.as_deref(),
            Some("{\"caption\":\"x\"}")
        );
    }
}
