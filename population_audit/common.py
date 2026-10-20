"""Shared helpers: polite HTTP GET with retry, hashing, failure log, JSON IO.

Read-only. No credentials, cookies or auth headers are ever sent or logged.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

UA = (
    "vwd-site-quality-audit/population-audit "
    "(read-only numeric audit; +https://github.com/happybird-yasu/vwd-site-quality-audit)"
)

_last_request = {}


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def http_get(url: str, *, host_key: str, min_interval: float, retries: int = 3,
             timeout: int = 180):
    """GET with a per-host minimum interval and bounded retries.

    Returns (body, headers, attempts). Raises the last exception after retries.
    """
    last_exc = None
    for attempt in range(1, retries + 1):
        wait = min_interval - (time.time() - _last_request.get(host_key, 0.0))
        if wait > 0:
            time.sleep(wait)
        _last_request[host_key] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return body, dict(resp.headers), attempt
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_exc = exc
            time.sleep(min(30, 5 * attempt))
    raise last_exc  # type: ignore[misc]


_SECRET = re.compile(r"(?i)(bearer\s+\S+|token=\S+|cookie[^,;]*|authorization[^,;]*)")


def safe_reason(exc: BaseException) -> str:
    """Exception type and message, secrets redacted and truncated."""
    return _SECRET.sub("[redacted]", f"{type(exc).__name__}: {exc}")[:300]


def log_failure(out_dir: Path, target: str, stage: str, exc: BaseException,
                retry_count: int, url: str | None = None) -> None:
    """Append one failure record (time, target, stage, url, error, retries). No credentials."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rec = {"ts": utcnow(), "target": target, "stage": stage, "url": url,
           "exception_type": type(exc).__name__, "reason": safe_reason(exc),
           "retry_count": retry_count}
    with open(out_dir / "failures.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def write_json(path: Path, obj, *, gz: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(obj, ensure_ascii=False, indent=None, separators=(",", ":"))
    if gz:
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            f.write(data)
    else:
        tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path):
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    return json.loads(path.read_text(encoding="utf-8"))
