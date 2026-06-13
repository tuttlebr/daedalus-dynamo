use std::net::SocketAddr;
use std::path::PathBuf;
use std::str::FromStr;

/// How prepared images are delivered to the inference backend.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ImageDelivery {
    /// Serve each prepared image over HTTP and hand the worker a URL. Use this
    /// for backends whose OpenAI-compatible image path accepts HTTP(S) URLs but
    /// rejects base64 `data:` URLs.
    Url,
    /// Inline each image as a base64 `data:` URL in the request body. Works with
    /// backends that accept data URLs.
    DataUrl,
}

#[derive(Clone, Debug)]
pub struct Config {
    pub listen_addr: SocketAddr,
    pub database_url: String,
    pub llm_endpoint: String,
    pub model_name: String,
    pub image_delivery: ImageDelivery,
    pub image_base_url: String,
    pub require_auth: bool,
    pub auth_user: String,
    pub auth_token: Option<String>,
    pub write_sidecar: bool,
    pub sidecar_roots: Vec<PathBuf>,
    pub max_concurrent_llm: usize,
    pub max_concurrent_hash: usize,
    pub max_file_bytes: u64,
    pub max_image_pixels: u64,
    pub max_edge_pixels: u32,
    pub downscale_large: bool,
    pub llm_timeout_secs: u64,
    pub llm_max_retries: u32,
    pub jpeg_quality: u8,
    pub allowed_roots: Vec<PathBuf>,
}

impl Config {
    pub fn from_env() -> Result<Self, String> {
        let write_sidecar = parse_bool_env("WRITE_SIDECAR", "true")?;
        let allowed_roots = parse_path_list("ALLOWED_ROOTS", "/volume1/homes,/volume2/daedalus")?;
        if allowed_roots.is_empty() {
            return Err("ALLOWED_ROOTS must contain at least one existing path".to_string());
        }

        let sidecar_default = if write_sidecar {
            "/volume2/daedalus"
        } else {
            ""
        };
        let sidecar_roots = parse_path_list("SIDECAR_ROOTS", sidecar_default)?;
        if write_sidecar {
            if sidecar_roots.is_empty() {
                return Err(
                    "SIDECAR_ROOTS must contain at least one path when WRITE_SIDECAR=true"
                        .to_string(),
                );
            }
            for sidecar_root in &sidecar_roots {
                if !allowed_roots
                    .iter()
                    .any(|allowed_root| sidecar_root.starts_with(allowed_root))
                {
                    return Err(format!(
                        "SIDECAR_ROOTS entry {} is outside ALLOWED_ROOTS {:?}",
                        sidecar_root.display(),
                        allowed_roots
                    ));
                }
            }
        }

        let max_concurrent_llm = parse_env::<usize>("MAX_CONCURRENT_LLM", "4")?;
        ensure_nonzero("MAX_CONCURRENT_LLM", max_concurrent_llm)?;
        let max_concurrent_hash = parse_env::<usize>("MAX_CONCURRENT_HASH", "8")?;
        ensure_nonzero("MAX_CONCURRENT_HASH", max_concurrent_hash)?;
        let max_file_bytes = parse_env::<u64>("MAX_FILE_BYTES", "104857600")?;
        ensure_nonzero("MAX_FILE_BYTES", max_file_bytes)?;
        let max_image_pixels = parse_env::<u64>("MAX_IMAGE_PIXELS", "80000000")?;
        ensure_nonzero("MAX_IMAGE_PIXELS", max_image_pixels)?;
        let max_edge_pixels = parse_env::<u32>("MAX_EDGE_PIXELS", "2048")?;
        ensure_nonzero("MAX_EDGE_PIXELS", max_edge_pixels)?;
        let llm_timeout_secs = parse_env::<u64>("LLM_TIMEOUT_SECS", "180")?;
        ensure_nonzero("LLM_TIMEOUT_SECS", llm_timeout_secs)?;
        let llm_max_retries = parse_env::<u32>("LLM_MAX_RETRIES", "3")?;
        ensure_nonzero("LLM_MAX_RETRIES", llm_max_retries)?;
        let jpeg_quality = parse_env::<u8>("JPEG_QUALITY", "85")?;
        if !(1..=100).contains(&jpeg_quality) {
            return Err(format!(
                "JPEG_QUALITY must be between 1 and 100, got {jpeg_quality}"
            ));
        }

        let image_delivery = parse_image_delivery("IMAGE_DELIVERY", "data-url")?;
        // Cluster-internal base URL the inference workers use to fetch images in
        // `url` delivery mode. Defaults to this service's in-namespace DNS name.
        let image_base_url = env_or("IMAGE_BASE_URL", "http://photo-tagger:3000")
            .trim_end_matches('/')
            .to_string();

        Config {
            listen_addr: env_or("LISTEN_ADDR", "0.0.0.0:3000")
                .parse()
                .map_err(|e| format!("Invalid LISTEN_ADDR: {e}"))?,
            database_url: env_or(
                "DATABASE_URL",
                "postgres://photo_tagger:photo_tagger@photo-tagger-postgres:5432/photo_tagger",
            ),
            llm_endpoint: required_env_any(&[
                "LLM_ENDPOINT",
                "QWEN_ENDPOINT",
                "NEMOTRON_ENDPOINT",
            ])?,
            model_name: required_env("MODEL_NAME")?,
            image_delivery,
            image_base_url,
            require_auth: parse_bool_env("PHOTO_TAGGER_REQUIRE_AUTH", "false")?,
            auth_user: env_or("PHOTO_TAGGER_AUTH_USER", "photo-tagger"),
            auth_token: optional_env("PHOTO_TAGGER_AUTH_TOKEN"),
            write_sidecar,
            sidecar_roots,
            max_concurrent_llm,
            max_concurrent_hash,
            max_file_bytes,
            max_image_pixels,
            max_edge_pixels,
            downscale_large: parse_bool_env("DOWNSCALE_LARGE", "true")?,
            llm_timeout_secs,
            llm_max_retries,
            jpeg_quality,
            allowed_roots,
        }
        .validate()
    }

    fn validate(self) -> Result<Self, String> {
        if self.require_auth && self.auth_token.as_deref().unwrap_or_default().is_empty() {
            return Err(
                "PHOTO_TAGGER_REQUIRE_AUTH=true but PHOTO_TAGGER_AUTH_TOKEN is not set".to_string(),
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

fn required_env_any(keys: &[&str]) -> Result<String, String> {
    keys.iter()
        .find_map(|key| optional_env(key))
        .ok_or_else(|| format!("One of {} is required", keys.join(", ")))
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

fn parse_image_delivery(key: &str, default: &str) -> Result<ImageDelivery, String> {
    match env_or(key, default).to_ascii_lowercase().as_str() {
        "url" | "http" | "http-url" => Ok(ImageDelivery::Url),
        "data-url" | "data_url" | "dataurl" | "base64" => Ok(ImageDelivery::DataUrl),
        value => Err(format!(
            "Invalid {key}: expected 'url' or 'data-url', got {value:?}"
        )),
    }
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

fn parse_path_list(key: &str, default: &str) -> Result<Vec<PathBuf>, String> {
    env_or(key, default)
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(|path| {
            std::fs::canonicalize(path).map_err(|e| format!("Invalid {key} path {path:?}: {e}"))
        })
        .collect()
}

fn ensure_nonzero<T>(key: &str, value: T) -> Result<(), String>
where
    T: PartialEq + From<u8> + std::fmt::Display,
{
    if value == T::from(0) {
        return Err(format!("{key} must be greater than zero"));
    }
    Ok(())
}
