use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use uuid::Uuid;

/// An in-memory, short-lived store of prepared image bytes that are served over
/// HTTP to the inference workers.
///
/// Dynamo's SGLang multimodal path only accepts HTTP(S) image URLs (not base64
/// `data:` URLs), so in `url` delivery mode the analyzer registers each prepared
/// image here, hands the worker a content-addressless random URL, and removes
/// the entry as soon as the LLM call returns (see [`ImageHandle`]). Entries are
/// therefore only fetchable for the lifetime of one `analyze()` call and are
/// bounded by `MAX_CONCURRENT_LLM` in-flight images.
#[derive(Debug, Default)]
pub struct ImageStore {
    inner: Mutex<HashMap<String, StoredImage>>,
}

#[derive(Debug, Clone)]
struct StoredImage {
    bytes: Arc<Vec<u8>>,
    content_type: &'static str,
}

impl ImageStore {
    pub fn new() -> Self {
        Self::default()
    }

    /// Register image bytes under a fresh random token and return an RAII handle.
    /// The entry is removed from the store when the handle is dropped.
    pub fn insert(self: &Arc<Self>, bytes: Vec<u8>, content_type: &'static str) -> ImageHandle {
        let token = Uuid::new_v4().simple().to_string();
        self.inner.lock().unwrap().insert(
            token.clone(),
            StoredImage {
                bytes: Arc::new(bytes),
                content_type,
            },
        );
        ImageHandle {
            store: Arc::clone(self),
            token,
        }
    }

    /// Look up image bytes by token, if still present.
    pub fn get(&self, token: &str) -> Option<(Arc<Vec<u8>>, &'static str)> {
        self.inner
            .lock()
            .unwrap()
            .get(token)
            .map(|img| (Arc::clone(&img.bytes), img.content_type))
    }

    fn remove(&self, token: &str) {
        self.inner.lock().unwrap().remove(token);
    }
}

/// RAII guard that removes its image from the store on drop, so a worker can
/// only fetch the image while the `analyze()` call that created it is running.
pub struct ImageHandle {
    store: Arc<ImageStore>,
    token: String,
}

impl ImageHandle {
    pub fn token(&self) -> &str {
        &self.token
    }
}

impl Drop for ImageHandle {
    fn drop(&mut self) {
        self.store.remove(&self.token);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn insert_get_then_drop_removes() {
        let store = Arc::new(ImageStore::new());
        let token = {
            let handle = store.insert(vec![1, 2, 3], "image/jpeg");
            let token = handle.token().to_string();
            let (bytes, ct) = store.get(&token).expect("present while handle alive");
            assert_eq!(*bytes, vec![1, 2, 3]);
            assert_eq!(ct, "image/jpeg");
            token
        };
        // Handle dropped at end of the block -> entry gone.
        assert!(store.get(&token).is_none());
    }

    #[test]
    fn tokens_are_unique() {
        let store = Arc::new(ImageStore::new());
        let a = store.insert(vec![0], "image/png");
        let b = store.insert(vec![0], "image/png");
        assert_ne!(a.token(), b.token());
    }
}
