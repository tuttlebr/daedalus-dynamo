use std::ffi::OsStr;
use std::path::{Component, Path, PathBuf};

use tokio::sync::mpsc;
use walkdir::{DirEntry, WalkDir};

const ALLOWED_EXTS: &[&str] = &[
    "jpg", "jpeg", "jpe", "jfif", "png", "webp", "gif", "bmp", "tif", "tiff", "heic", "heif",
];

#[derive(Debug, Clone)]
pub struct WalkConfig {
    pub root: PathBuf,
    pub recursive: bool,
}

#[derive(Debug, Default)]
pub struct WalkSummary {
    pub errors: u64,
}

/// Walks `root` synchronously inside `spawn_blocking` and pushes matching paths
/// into `tx`. Returns when the walk completes (or the receiver disappears).
pub async fn walk_into(cfg: WalkConfig, tx: mpsc::Sender<PathBuf>) -> Result<WalkSummary, String> {
    tokio::task::spawn_blocking(move || {
        let mut summary = WalkSummary::default();
        let mut walker = WalkDir::new(&cfg.root).follow_links(false);
        if !cfg.recursive {
            walker = walker.max_depth(1);
        }
        for entry in walker
            .into_iter()
            .filter_entry(|entry| !is_ignored_dir(entry))
        {
            let entry = match entry {
                Ok(entry) => entry,
                Err(err) => {
                    summary.errors += 1;
                    tracing::warn!(error = %err, "directory walk entry failed");
                    continue;
                }
            };
            if !entry.file_type().is_file() {
                continue;
            }
            let p = entry.into_path();
            if has_ignored_component(&p) {
                continue;
            }
            if is_sidecar(&p) {
                continue;
            }
            if !is_allowed(&p) {
                continue;
            }
            if tx.blocking_send(p).is_err() {
                break;
            }
        }
        summary
    })
    .await
    .map_err(|e| format!("walk task join error: {e}"))
}

fn is_allowed(p: &Path) -> bool {
    p.extension()
        .and_then(|e| e.to_str())
        .map(|e| {
            ALLOWED_EXTS
                .iter()
                .any(|allowed| allowed.eq_ignore_ascii_case(e))
        })
        .unwrap_or(false)
}

fn is_sidecar(p: &Path) -> bool {
    p.file_name()
        .and_then(|n| n.to_str())
        .map(|n| n.ends_with(".metadata.json"))
        .unwrap_or(false)
}

fn is_ignored_dir(entry: &DirEntry) -> bool {
    entry.file_type().is_dir() && is_ignored_name(entry.file_name())
}

fn has_ignored_component(p: &Path) -> bool {
    p.components().any(|component| match component {
        Component::Normal(name) => is_ignored_name(name),
        _ => false,
    })
}

fn is_ignored_name(name: &OsStr) -> bool {
    name.to_str()
        .map(|name| name.eq_ignore_ascii_case("@eaDir"))
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn allow_check() {
        assert!(is_allowed(Path::new("a/b.JPG")));
        assert!(is_allowed(Path::new("a/b.png")));
        assert!(is_allowed(Path::new("a/b.WebP")));
        assert!(!is_allowed(Path::new("a/b.txt")));
        assert!(!is_allowed(Path::new("a/b")));
    }

    #[test]
    fn sidecar_check() {
        assert!(is_sidecar(Path::new("/x/foo.jpg.metadata.json")));
        assert!(!is_sidecar(Path::new("/x/foo.jpg")));
    }

    #[test]
    fn ignored_synology_metadata_check() {
        assert!(has_ignored_component(Path::new(
            "/x/@eaDir/foo.jpg/SYNOPHOTO_THUMB_M.jpg"
        )));
        assert!(has_ignored_component(Path::new("/x/@EADIR/foo.jpg")));
        assert!(!has_ignored_component(Path::new("/x/album/foo.jpg")));
    }
}
