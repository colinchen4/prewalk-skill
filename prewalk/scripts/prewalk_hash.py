"""Content fingerprints and change-only writes for Prewalk artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import prewalk_runtime


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_hash(value: Any, exclude: Iterable[str] = ()) -> str:
    ignored = set(exclude)
    payload = {key: item for key, item in value.items() if key not in ignored}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_text(encoded)


def write_json_if_changed(path: Path, value: Any, exclude: Iterable[str] = ()) -> tuple[bool, str]:
    fingerprint = content_hash(value, exclude)
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        current = None
    if isinstance(current, dict) and content_hash(current, exclude) == fingerprint:
        return True, fingerprint
    prewalk_runtime._atomic_write_json(path, value)
    return False, fingerprint


def write_text_if_changed(path: Path, text: str) -> bool:
    fingerprint = sha256_text(text)
    try:
        if sha256_file(path) == fingerprint:
            return True
    except FileNotFoundError:
        pass
    prewalk_runtime._atomic_write_text(path, text)
    return False
