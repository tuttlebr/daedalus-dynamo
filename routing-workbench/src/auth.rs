use std::sync::Arc;

use axum::extract::{Request, State};
use axum::http::{header, HeaderMap, HeaderValue, StatusCode};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use axum::Json;
use base64::Engine;

use crate::config::Config;
use crate::routes::AppState;

pub async fn require_auth(
    State(state): State<Arc<AppState>>,
    req: Request,
    next: Next,
) -> Result<Response, Response> {
    let cfg = &state.cfg;
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
        HeaderValue::from_static("Basic realm=\"Dynamo Routing Workbench\""),
    );
    Err(response)
}

fn is_authorized(headers: &HeaderMap, cfg: &Config) -> bool {
    let Some(token) = cfg.auth_token.as_deref() else {
        return false;
    };

    if headers
        .get("x-workbench-token")
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

#[cfg(test)]
mod tests {
    use super::constant_time_eq;

    #[test]
    fn constant_time_eq_matches_equal_inputs() {
        assert!(constant_time_eq(b"token", b"token"));
        assert!(!constant_time_eq(b"token", b"other"));
        assert!(!constant_time_eq(b"token", b"token-longer"));
    }
}
