"""Pinned public Super cache integrity; stdlib-only until explicit download_main()."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile

MODEL = "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4"
REVISION = "da5b2650a835292c678994a1fa3fb653250fb508"
CONFIG_SHA = "35189a8038d1b1f934e58cba50eb7e97b42a4da569424e35b9fd1048058bc7ea"
INDEX_SHA = "c368acdbcfe882236af1ec79df4a1fb6127e6ed9f742d59c01c21fe96ddc2741"
CACHE_ROOT = Path("/model/hf")
REPO_DIR = "models--nvidia--NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4"
MAX_DOWNLOAD_BYTES = 112 * 1024**3
MAX_FILES = 4096
WEIGHT_COUNT = 88
WEIGHT_BYTES = 75010188240
MARKER = ".stage-verified.json"


class IntegrityError(RuntimeError):
    """The cache has not earned a usable completion marker."""


def require(condition, message):
    if not condition:
        raise IntegrityError(message)


def safe_name(name):
    require(isinstance(name, str) and bool(name), "invalid repository path")
    require(not name.startswith("/") and "\\" not in name, "unsafe repository path")
    require(
        all(p not in ("", ".", "..") for p in name.split("/")), "unsafe path component"
    )
    require(not any(ord(c) < 32 or ord(c) == 127 for c in name), "unsafe path control")
    require(
        not any(p.startswith(".stage-verified") for p in PurePosixPath(name).parts),
        "reserved marker path",
    )
    return name


def integer_size(value):
    require(
        type(value) is int and 0 <= value <= MAX_DOWNLOAD_BYTES, "invalid file size"
    )
    return value


def field(value, name, default=None):
    return (
        value.get(name, default)
        if isinstance(value, dict)
        else getattr(value, name, default)
    )


def metadata_files(info):
    """Check complete public pinned metadata and byte ceiling BEFORE any file fetch."""
    require(
        field(info, "id") == MODEL and field(info, "sha") == REVISION,
        "wrong repository/revision",
    )
    require(
        field(info, "private") is False and field(info, "gated") is False,
        "repository is not public/ungated",
    )
    siblings = field(info, "siblings")
    require(
        isinstance(siblings, list) and 0 < len(siblings) <= MAX_FILES,
        "invalid metadata file count",
    )
    result = {}
    total = 0
    for entry in siblings:
        name = safe_name(field(entry, "rfilename"))
        require(name not in result, "duplicate repository path")
        size = integer_size(field(entry, "size"))
        lfs = field(entry, "lfs")
        if lfs is not None:
            digest = field(lfs, "sha256")
            require(field(lfs, "size", size) == size, "LFS size mismatch")
            algorithm = "sha256"
            require(
                isinstance(digest, str) and re.fullmatch("[0-9a-f]{64}", digest),
                "missing LFS SHA256",
            )
        else:
            digest = field(entry, "blob_id")
            algorithm = "git_blob_sha1"
            require(
                isinstance(digest, str) and re.fullmatch("[0-9a-f]{40}", digest),
                "missing Git blob SHA1",
            )
        result[name] = {"size": size, "algorithm": algorithm, "digest": digest}
        total += size
        require(total <= MAX_DOWNLOAD_BYTES, "repository footprint exceeds 112 GiB")
    require(
        "config.json" in result and "model.safetensors.index.json" in result,
        "missing pinned configuration/index",
    )
    return result


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def invalidate_marker(snapshot):
    snapshot.mkdir(parents=True, exist_ok=True)
    marker = snapshot / MARKER
    if marker.exists() or marker.is_symlink():
        marker.unlink()
    for temp in snapshot.glob(".stage-verified.*.tmp"):
        require(
            temp.is_file() and not temp.is_symlink(), "unsafe stale marker temporary"
        )
        temp.unlink()
    # Persist removal before a metadata/file attempt can start.
    fsync_directory(snapshot)


def digest_file(path, expected):
    size = integer_size(expected.get("size"))
    require(
        path.is_file() and path.stat().st_size == size, "missing file or wrong size"
    )
    sha256 = hashlib.sha256()
    git_sha1 = hashlib.sha1(
        b"blob " + str(size).encode("ascii") + b"\0", usedforsecurity=False
    )
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            sha256.update(chunk)
            git_sha1.update(chunk)
    algorithm = expected.get("algorithm")
    actual = {"sha256": sha256.hexdigest(), "git_blob_sha1": git_sha1.hexdigest()}.get(
        algorithm
    )
    require(
        actual is not None and actual == expected.get("digest"),
        "immutable file digest mismatch",
    )
    return sha256.hexdigest()


def contained_file(snapshot, name, cache_root):
    target = snapshot / safe_name(name)
    require(
        target.resolve().is_relative_to(cache_root.resolve()),
        "cache path escapes owned claim",
    )
    return target


def read_index(
    snapshot,
    cache_root,
    expected_config=CONFIG_SHA,
    expected_index=INDEX_SHA,
    expected_weight_count=WEIGHT_COUNT,
    expected_weight_bytes=WEIGHT_BYTES,
):
    for name, expected in [
        ("config.json", expected_config),
        ("model.safetensors.index.json", expected_index),
    ]:
        require(
            hashlib.sha256(
                contained_file(snapshot, name, cache_root).read_bytes()
            ).hexdigest()
            == expected,
            "pinned configuration/index SHA256 mismatch",
        )
    index = json.loads((snapshot / "model.safetensors.index.json").read_text())
    require(
        isinstance(index, dict) and isinstance(index.get("weight_map"), dict),
        "invalid weight index",
    )
    names = {safe_name(n) for n in index["weight_map"].values()}
    require(len(names) == expected_weight_count, "unexpected index weight count")
    require(
        all(n.endswith(".safetensors") for n in names), "non-safetensors index entry"
    )
    require(
        sum(contained_file(snapshot, n, cache_root).stat().st_size for n in names)
        == expected_weight_bytes,
        "unexpected indexed weight bytes",
    )
    return names


def verify_snapshot(snapshot, cache_root, files, **index_expectations):
    require(0 < len(files) <= MAX_FILES, "invalid manifest file count")
    require(
        sum(integer_size(m.get("size")) for m in files.values()) <= MAX_DOWNLOAD_BYTES,
        "manifest exceeds 112 GiB",
    )
    actual_names = {
        str(p.relative_to(snapshot))
        for p in snapshot.rglob("*")
        if p.is_file() and p != snapshot / MARKER
    }
    require(actual_names == set(files), "unexpected or missing snapshot files")
    verified = {}
    for name, expected in sorted(files.items()):
        sha = digest_file(contained_file(snapshot, name, cache_root), expected)
        verified[name] = {**expected, "sha256": sha}
    weights = read_index(snapshot, cache_root, **index_expectations)
    require(weights <= set(verified), "unverified index weight file")
    return {
        "schema_version": 1,
        "model": MODEL,
        "revision": REVISION,
        "verified_weight_files": len(weights),
        "weight_file_bytes": sum(verified[n]["size"] for n in weights),
        "download_file_bytes": sum(v["size"] for v in verified.values()),
        "verified_files": verified,
    }


def publish_marker(snapshot, manifest):
    """Only an fsynced valid JSON document, atomic rename and directory fsync earn completion."""
    data = (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8")
    require(json.loads(data) == manifest, "marker JSON roundtrip failed")
    path = snapshot / MARKER
    require(
        not path.exists() and not path.is_symlink(), "stale marker was not invalidated"
    )
    fd, temp_name = tempfile.mkstemp(
        prefix=".stage-verified.", suffix=".tmp", dir=snapshot
    )
    temp = Path(temp_name)
    renamed = False
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        renamed = True
        fsync_directory(snapshot)
    except BaseException:
        # In particular, a failed directory fsync must not leave apparent success.
        if renamed:
            path.unlink(missing_ok=True)
            try:
                fsync_directory(snapshot)
            except OSError:
                pass
        raise
    finally:
        temp.unlink(missing_ok=True)


def validate_offline(snapshot, cache_root, **index_expectations):
    """Before remote code/tokenizer import: verify every support hash and all weight sizes."""
    marker = snapshot / MARKER
    require(marker.is_file() and not marker.is_symlink(), "completion marker missing")
    require(marker.stat().st_size <= 4 * 1024 * 1024, "marker too large")
    raw = marker.read_bytes()
    require(raw.endswith(b"\n"), "incomplete marker JSON")
    data = json.loads(raw)
    require(
        data.get("schema_version") == 1
        and data.get("model") == MODEL
        and data.get("revision") == REVISION,
        "wrong marker scope",
    )
    files = data.get("verified_files")
    require(
        isinstance(files, dict) and 0 < len(files) <= MAX_FILES,
        "invalid recorded files",
    )
    require(
        sum(integer_size(v.get("size")) for v in files.values())
        == data.get("download_file_bytes")
        <= MAX_DOWNLOAD_BYTES,
        "invalid recorded byte bound",
    )
    weights = read_index(snapshot, cache_root, **index_expectations)
    require(
        len(weights) == data.get("verified_weight_files")
        and sum(files[n]["size"] for n in weights) == data.get("weight_file_bytes"),
        "invalid recorded weights",
    )
    actual_names = {
        str(p.relative_to(snapshot))
        for p in snapshot.rglob("*")
        if p.is_file() and p != snapshot / MARKER
    }
    require(actual_names == set(files), "unrecorded snapshot file")
    for name, expected in files.items():
        target = contained_file(snapshot, name, cache_root)
        require(
            target.is_file() and target.stat().st_size == expected.get("size"),
            "cached file size changed",
        )
        if name not in weights:
            # Recheck immutable LFS/Git metadata AND the recorded SHA256, including .py/templates/tokenizers.
            require(
                digest_file(target, expected) == expected.get("sha256"),
                "support SHA256 changed",
            )
    return data


def download_main():
    # No token/config inspection. The exact public revision is the only allowed source.
    snapshot = CACHE_ROOT / REPO_DIR / "snapshots" / REVISION
    require(
        snapshot.resolve().is_relative_to(CACHE_ROOT.resolve()),
        "snapshot is outside owned cache",
    )
    invalidate_marker(snapshot)
    from huggingface_hub import HfApi, snapshot_download

    info = HfApi(endpoint="https://huggingface.co", token=False).model_info(
        MODEL, revision=REVISION, files_metadata=True
    )
    files = metadata_files(info)
    require(
        os.statvfs(CACHE_ROOT).f_bavail * os.statvfs(CACHE_ROOT).f_frsize
        >= sum(f["size"] for f in files.values()),
        "insufficient free space before fetch",
    )
    fetched = Path(
        snapshot_download(
            MODEL,
            revision=REVISION,
            cache_dir=str(CACHE_ROOT),
            token=False,
            endpoint="https://huggingface.co",
            allow_patterns=sorted(files),
            max_workers=4,
        )
    )
    require(fetched.absolute() == snapshot.absolute(), "unexpected snapshot path")
    manifest = verify_snapshot(snapshot, CACHE_ROOT, files)
    publish_marker(snapshot, manifest)
    print(
        json.dumps(
            {
                "revision": REVISION,
                "verified_weight_files": manifest["verified_weight_files"],
                "weight_file_bytes": manifest["weight_file_bytes"],
                "download_file_bytes": manifest["download_file_bytes"],
            }
        )
    )


# DOWNLOAD_ENTRYPOINT -- excluded when embedding the offline startup guard.
if __name__ == "__main__":
    download_main()
