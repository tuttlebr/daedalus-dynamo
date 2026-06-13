use std::net::SocketAddr;
use std::str::FromStr;

#[derive(Clone, Debug)]
pub struct Config {
    pub listen_addr: SocketAddr,
    pub openai_base_url: String,
    pub default_model: String,
    pub require_auth: bool,
    pub auth_user: String,
    pub auth_token: Option<String>,
    pub request_timeout_secs: u64,
    pub max_run_requests: usize,
    pub max_concurrency: usize,
}

impl Config {
    pub fn from_env() -> Result<Self, String> {
        let cfg = Config {
            listen_addr: env_or("LISTEN_ADDR", "0.0.0.0:3000")
                .parse()
                .map_err(|e| format!("Invalid LISTEN_ADDR: {e}"))?,
            openai_base_url: env_or("OPENAI_BASE_URL", "http://inference-gateway:80/v1")
                .trim_end_matches('/')
                .to_string(),
            default_model: required_env("WORKBENCH_DEFAULT_MODEL")
                .or_else(|_| required_env("MODEL_NAME"))?,
            require_auth: parse_bool_env("WORKBENCH_REQUIRE_AUTH", "false")?,
            auth_user: env_or("WORKBENCH_AUTH_USER", "routing-workbench"),
            auth_token: optional_env("WORKBENCH_AUTH_TOKEN"),
            request_timeout_secs: parse_env("WORKBENCH_REQUEST_TIMEOUT_SECS", "300")?,
            max_run_requests: parse_env("WORKBENCH_MAX_RUN_REQUESTS", "128")?,
            max_concurrency: parse_env("WORKBENCH_MAX_CONCURRENCY", "16")?,
        };
        cfg.validate()
    }

    fn validate(self) -> Result<Self, String> {
        if self.openai_base_url.is_empty() {
            return Err("OPENAI_BASE_URL must not be empty".to_string());
        }
        if self.default_model.trim().is_empty() {
            return Err("WORKBENCH_DEFAULT_MODEL must not be empty".to_string());
        }
        if self.request_timeout_secs == 0 {
            return Err("WORKBENCH_REQUEST_TIMEOUT_SECS must be greater than zero".to_string());
        }
        if self.max_run_requests == 0 {
            return Err("WORKBENCH_MAX_RUN_REQUESTS must be greater than zero".to_string());
        }
        if self.max_concurrency == 0 {
            return Err("WORKBENCH_MAX_CONCURRENCY must be greater than zero".to_string());
        }
        if self.require_auth && self.auth_token.as_deref().unwrap_or_default().is_empty() {
            return Err(
                "WORKBENCH_REQUIRE_AUTH=true but WORKBENCH_AUTH_TOKEN is not set".to_string(),
            );
        }
        Ok(self)
    }
}

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}

fn required_env(key: &str) -> Result<String, String> {
    optional_env(key).ok_or_else(|| format!("{key} is required"))
}

fn optional_env(key: &str) -> Option<String> {
    std::env::var(key)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

fn parse_env<T>(key: &str, default: &str) -> Result<T, String>
where
    T: FromStr,
    T::Err: std::fmt::Display,
{
    let value = env_or(key, default);
    value.parse().map_err(|e| format!("Invalid {key}: {e}"))
}

fn parse_bool_env(key: &str, default: &str) -> Result<bool, String> {
    match env_or(key, default).to_ascii_lowercase().as_str() {
        "1" | "true" | "yes" | "on" => Ok(true),
        "0" | "false" | "no" | "off" => Ok(false),
        value => Err(format!(
            "Invalid {key}: expected true or false, got {value:?}"
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::parse_bool_env;

    #[test]
    fn bool_parser_accepts_common_values() {
        std::env::set_var("WORKBENCH_TEST_BOOL", "yes");
        assert!(parse_bool_env("WORKBENCH_TEST_BOOL", "false").unwrap());
        std::env::set_var("WORKBENCH_TEST_BOOL", "0");
        assert!(!parse_bool_env("WORKBENCH_TEST_BOOL", "true").unwrap());
        std::env::remove_var("WORKBENCH_TEST_BOOL");
    }
}
