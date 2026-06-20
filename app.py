#!/usr/bin/env python3
import asyncio
import base64
import contextlib
import csv
import glob
import html
import io
import json
import logging
import os
import re
import secrets
import signal
import subprocess
import time
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
import psutil
import yaml
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

logger = logging.getLogger("obbystreams")
CONFIG_PATH = Path(os.environ.get("OBBYSTREAMS_CONFIG", "/etc/obbystreams/obbystreams.yaml"))
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
APP_STARTED_AT = None

# Config read cache: disk reads happen on every request in some paths; cache for
# a short TTL to avoid I/O overhead while still picking up edits quickly.
_CONFIG_CACHE: dict = {"config": None, "mtime": 0.0, "at": 0.0}
_CONFIG_CACHE_TTL = 1.0

# Shared HTTP client for upstream HLS fetches (avoids creating a new client per
# request in hls_proxy).
_HTTPX_CLIENT: httpx.AsyncClient | None = None

EVENTS: deque[dict] = deque(maxlen=300)
LOGS: deque[dict] = deque(maxlen=600)
ERRORS: deque[dict] = deque(maxlen=200)
PROCESS = None
STARTED_AT = None
READER_TASK = None
PROCESS_LOCK = asyncio.Lock()
WATCHDOG_TASK = None
WATCHDOG_LAST_ACTION = 0.0
STREAM_DESIRED_STATE = "running"
NVIDIA_SMI_CACHE_SECONDS = 5.0
NVIDIA_SMI_CACHE: dict = {"at": 0.0, "payload": None}
NVIDIA_SMI_LOCK = asyncio.Lock()
ARANGO_WORKER_TASK = None
ARANGO_QUEUE_MAX = 1200
ARANGO_QUEUE: asyncio.Queue | None = None
ARANGO_RETRY_MAX_ATTEMPTS = 3

# Auto-scraped public stream sources (hereisman playlist URLs)
_AUTO_SOURCES: list[str] = []
_AUTO_SOURCES_AT: float = 0.0
_AUTO_SOURCES_LOCK: asyncio.Lock | None = None  # initialised in lifespan
_AUTO_SCRAPE_INTERVAL = 300  # seconds between refreshes
_AUTO_SCRAPE_TASK = None

SOURCE_HEALTH_TASK = None
SOURCE_HEALTH_INTERVAL = 15
SOURCE_HEALTH: dict[str, dict] = {}
VIEWER_SESSION_TTL = 45
VIEWER_SESSIONS: dict[str, dict] = {}
VIEWER_LOCK = asyncio.Lock()


RUNTIME = {
    "stream_starts": 0,
    "stream_restarts": 0,
    "watchdog_restarts": 0,
    "start_failures": 0,
    "last_exit_code": None,
    "arango_dropped_writes": 0,
    "arango_write_failures": 0,
}


class _ProxyCache:
    """Bounded TTL cache for HLS proxy responses with coalesced fetches.

    The previous implementation kept every unique URL in `_PROXY_INFLIGHT`
    forever, and only evicted expired `_PROXY_CACHE` entries when the cache
    grew past 500 items. Live HLS segments use unique signed URLs, so both
    dicts grew without bound, eventually consuming CPU/memory and hanging
    the event loop. This class caps size, removes inflight locks after use,
    and prunes expired entries eagerly. Cache mutations are serialized with
    an internal lock so concurrent async tasks cannot corrupt the dicts.
    """

    def __init__(self, max_size: int = 5000, playlist_ttl: float = 2.5, segment_ttl: float = 120.0, stale_ttl: float = 600.0):
        self._cache: dict[str, dict] = {}
        self._inflight: dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()
        self._max_size = max(max_size, 1)
        self._playlist_ttl = playlist_ttl
        self._segment_ttl = segment_ttl
        self._stale_ttl = max(stale_ttl, 0.0)
        self._stats = {
            "hits": 0,
            "stale_hits": 0,
            "misses": 0,
            "sets": 0,
            "evictions": 0,
            "upstream_fetches": 0,
            "upstream_errors": 0,
            "bytes": 0,
        }

    async def get(self, raw_url: str, now: float) -> tuple[bytes, str] | None:
        cached = self._cache.get(raw_url)
        if cached and cached["expires_at"] > now:
            self._stats["hits"] += 1
            return cached["body"], cached["content_type"]
        # Eagerly drop this single expired entry if we see it.
        if cached and cached["stale_until"] <= now:
            async with self._lock:
                removed = self._cache.pop(raw_url, None)
                if removed:
                    self._stats["bytes"] = max(0, self._stats["bytes"] - len(removed["body"]))
                    self._stats["evictions"] += 1
        self._stats["misses"] += 1
        return None

    async def get_stale(self, raw_url: str, now: float) -> tuple[bytes, str] | None:
        cached = self._cache.get(raw_url)
        if cached and cached["stale_until"] > now:
            self._stats["stale_hits"] += 1
            return cached["body"], cached["content_type"]
        return None

    def lock(self, raw_url: str) -> asyncio.Lock:
        return self._inflight.setdefault(raw_url, asyncio.Lock())

    async def set(self, raw_url: str, body: bytes, ct: str, ttl: float, now: float, stale_ttl: float | None = None) -> None:
        stale_ttl = self._stale_ttl if stale_ttl is None else max(stale_ttl, 0.0)
        async with self._lock:
            previous = self._cache.get(raw_url)
            if previous:
                self._stats["bytes"] = max(0, self._stats["bytes"] - len(previous["body"]))
            self._cache[raw_url] = {
                "expires_at": now + ttl,
                "stale_until": now + ttl + stale_ttl,
                "body": body,
                "content_type": ct,
            }
            self._stats["sets"] += 1
            self._stats["bytes"] += len(body)
            if len(self._cache) > self._max_size:
                await self._cleanup_unlocked(now)
                # If we are still over the limit, evict the oldest half.
                if len(self._cache) > self._max_size:
                    keys = sorted(self._cache.keys(), key=lambda key: self._cache[key]["stale_until"])
                    for k in keys[: max(1, len(keys) // 2)]:
                        removed = self._cache.pop(k, None)
                        if removed:
                            self._stats["bytes"] = max(0, self._stats["bytes"] - len(removed["body"]))
                            self._stats["evictions"] += 1

    async def release_lock(self, raw_url: str, lock: asyncio.Lock) -> None:
        async with self._lock:
            if self._inflight.get(raw_url) is lock and not lock.locked():
                self._inflight.pop(raw_url, None)

    def record_upstream_fetch(self) -> None:
        self._stats["upstream_fetches"] += 1

    def record_upstream_error(self) -> None:
        self._stats["upstream_errors"] += 1

    def stats(self) -> dict:
        return {
            **self._stats,
            "entries": len(self._cache),
            "inflight": len(self._inflight),
            "max_size": self._max_size,
            "playlist_ttl": self._playlist_ttl,
            "segment_ttl": self._segment_ttl,
            "stale_ttl": self._stale_ttl,
        }

    async def cleanup(self) -> None:
        async with self._lock:
            await self._cleanup_unlocked(time.monotonic())

    async def _cleanup_unlocked(self, now: float) -> None:
        # Snapshot keys first to avoid "dictionary changed size during iteration"
        # if another task mutates the cache while we clean.
        for k in list(self._cache.keys()):
            entry = self._cache.get(k)
            if entry and entry["stale_until"] <= now:
                removed = self._cache.pop(k, None)
                if removed:
                    self._stats["bytes"] = max(0, self._stats["bytes"] - len(removed["body"]))
                    self._stats["evictions"] += 1
        for k in list(self._inflight.keys()):
            lock = self._inflight.get(k)
            if lock and not lock.locked():
                self._inflight.pop(k, None)

    def ttl_for(self, body: bytes, ct: str, raw_url: str) -> float:
        is_playlist = (
            "mpegurl" in ct.lower()
            or "m3u" in ct.lower()
            or raw_url.split("?")[0].endswith(".m3u8")
            or body.lstrip()[:7] == b"#EXTM3U"
        )
        return self._playlist_ttl if is_playlist else self._segment_ttl


_PROXY_CACHE = _ProxyCache()


def now_ms():
    return int(time.time() * 1000)


APP_STARTED_AT = now_ms()

ENCODER_CHOICES = {
    "auto",
    "gpu",
    "gpu-only",
    "gpu-trans",
    "nv",
    "nvidia",
    "nvenc",
    "nv-gpu-trans",
    "intel",
    "qsv",
    "intel-gpu-trans",
    "amd",
    "amf",
    "amd-gpu-trans",
    "vaapi",
    "cpu",
}


DEFAULT_CONFIG = {
    "server": {"host": "127.0.0.1", "port": 8767, "workers": 1},
    "dashboard": {"password": "", "session_token": ""},
    "stream": {
        "command": "/usr/bin/obbystreams",
        "encoder": "auto",
        "output_dir": "/var/www/live.obnoxious.lol/stream",
        "ffmpeg_log_dir": "ffmpegLogs",
        "scrape_url": "",
        "scrape_urls": [],
        "public_dash_url": "",
        "public_hls_url": "",
        "bitrate": "8M",
        "bitrate_720": "3500k",
        "maxrate_720": "4500k",
        "bufsize_720": "9000k",
        "maxrate_1080": "12M",
        "bufsize_1080": "24M",
        "audio_bitrate": "192k",
        "include_auto_public_sources": True,
        "source_manifest_path": "/tmp/obbystreams-sources.json",
        "soursignal_auto_recover": True,
        "auto_recover": True,
        "auto_restart_on_exit": True,
        "watchdog_restart_cooldown": 20,
        "startup_grace_seconds": 25,
        "playlist_stale_seconds": 25,
        "min_assessment_seconds": 15,
        "health_sample_interval": 2,
        "success_score_threshold": 180,
        "failure_score_threshold": -120,
        "confirmed_failure_samples": 2,
        "failure_ramp_seconds": 60,
        "links": [],
        "sources": [],
    },
    "public_sources": [],
    "arangodb": {
        "enabled": True,
        "url": "http://127.0.0.1:8529",
        "database": "obbystreams",
        "username": "obbystreams_app",
        "password": "",
    },
}


def safe_number(value, fallback, minimum=None):
    try:
        n = float(value)
    except (TypeError, ValueError):
        n = float(fallback)
    if minimum is not None:
        n = max(float(minimum), n)
    return n


def safe_int(value, fallback, minimum=None):
    return int(safe_number(value, fallback, minimum=minimum))


def safe_float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def smi_text(value):
    text = str(value or "").strip()
    if text in {"", "N/A", "[N/A]", "Not Supported", "[Not Supported]", "-"}:
        return None
    return text


def smi_float(value):
    text = smi_text(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def smi_int(value):
    number = smi_float(value)
    if number is None:
        return None
    return int(number)


def smi_percent(part, whole):
    if part is None or whole in (None, 0):
        return None
    return round((float(part) / float(whole)) * 100, 1)


def valid_stream_url(value):
    text = str(value or "").strip()
    if not text:
        return False
    parsed = urlparse(text)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def request_origin(request):
    origin = (request.headers.get("origin") or "").strip()
    if origin:
        return origin
    referer = (request.headers.get("referer") or "").strip()
    if not referer:
        return ""
    parsed = urlparse(referer)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def trusted_request_origin(request):
    candidate = request_origin(request)
    if not candidate:
        return False
    parsed = urlparse(candidate)
    request_url = request.url
    return parsed.scheme == request_url.scheme and parsed.netloc == request_url.netloc


def normalize_links(raw_links):
    links = []
    seen = set()
    for item in raw_links or []:
        candidate = str(item).strip()
        if not valid_stream_url(candidate):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        links.append(candidate)
    return links


def source_type_for_url(raw_type, url):
    candidate = str(raw_type or "").strip().lower()
    if candidate in {"hls", "soursignal", "page", "external"}:
        return candidate
    host = urlparse(str(url or "")).netloc.lower()
    if host.endswith("soursignal.com"):
        return "soursignal"
    return "hls"


def normalize_source_headers(raw_headers):
    if not isinstance(raw_headers, dict):
        return {}
    headers = {}
    for key, value in raw_headers.items():
        name = str(key or "").strip()
        if not name or any(ch in name for ch in "\r\n:"):
            continue
        headers[name] = str(value or "").strip()
    return headers


def normalize_sources(raw_sources, fallback_links=None):
    sources = []
    seen_ids = set()
    seen_urls = set()
    values = raw_sources if isinstance(raw_sources, list) else []
    for index, item in enumerate(values):
        if isinstance(item, str):
            raw = {"url": item}
        elif isinstance(item, dict):
            raw = item
        else:
            continue
        url = str(raw.get("url") or raw.get("link") or "").strip()
        if not valid_stream_url(url) or url in seen_urls:
            continue
        raw_id = str(raw.get("id") or "").strip()
        source_id = re.sub(r"[^a-zA-Z0-9_.-]+", "-", raw_id).strip("-").lower()
        if not source_id:
            source_id = f"source-{index + 1}"
        base_id = source_id
        suffix = 2
        while source_id in seen_ids:
            source_id = f"{base_id}-{suffix}"
            suffix += 1
        label = str(raw.get("label") or raw.get("name") or f"Source {len(sources) + 1}").strip()
        source = {
            "id": source_id,
            "label": label,
            "url": url,
            "type": source_type_for_url(raw.get("type"), url),
            "enabled": bool(raw.get("enabled", True)),
            "headers": normalize_source_headers(raw.get("headers")),
        }
        notes = str(raw.get("notes") or "").strip()
        if notes:
            source["notes"] = notes
        seen_ids.add(source_id)
        seen_urls.add(url)
        sources.append(source)
    for url in normalize_links(fallback_links or []):
        if url in seen_urls:
            continue
        source = {
            "id": f"source-{len(sources) + 1}",
            "label": f"Source {len(sources) + 1}",
            "url": url,
            "type": source_type_for_url(None, url),
            "enabled": True,
            "headers": {},
        }
        seen_ids.add(source["id"])
        seen_urls.add(url)
        sources.append(source)
    return sources


def normalize_public_sources(raw_sources):
    sources = []
    seen_ids = set()
    seen_urls = set()
    values = raw_sources if isinstance(raw_sources, list) else []
    for index, item in enumerate(values):
        if isinstance(item, str):
            raw = {"url": item}
        elif isinstance(item, dict):
            raw = item
        else:
            continue
        url = str(raw.get("url") or raw.get("link") or "").strip()
        if not valid_stream_url(url) or url in seen_urls:
            continue
        raw_id = str(raw.get("id") or "").strip()
        source_id = re.sub(r"[^a-zA-Z0-9_.-]+", "-", raw_id).strip("-").lower()
        if not source_id:
            source_id = f"public-{index + 1}"
        base_id = source_id
        suffix = 2
        while source_id in seen_ids:
            source_id = f"{base_id}-{suffix}"
            suffix += 1
        source = {
            "id": source_id,
            "label": str(raw.get("label") or f"Public {len(sources) + 1}").strip(),
            "url": url,
            "enabled": bool(raw.get("enabled", True)),
            "type": str(raw.get("type") or "public-hls").strip() or "public-hls",
            "origin": "manual",
            "read_only": False,
            "headers": normalize_source_headers(raw.get("headers")),
        }
        description = str(raw.get("description") or raw.get("notes") or "").strip()
        if description:
            source["description"] = description
        seen_ids.add(source_id)
        seen_urls.add(url)
        sources.append(source)
    return sources


def proxied_public_source(source):
    safe = {key: value for key, value in source.items() if key != "headers"}
    if source.get("headers"):
        safe["has_headers"] = True
    return {
        **safe,
        "playback_url": _proxy_url(source.get("url", "")),
    }


def auto_public_sources():
    return [
        {
            "id": f"auto-public-{index + 1}",
            "label": f"Auto public {index + 1}",
            "url": url,
            "enabled": True,
            "type": "auto-public-hls",
            "origin": "auto",
            "read_only": True,
            "description": "Auto-discovered from configured public scrape pages.",
        }
        for index, url in enumerate(current_auto_sources())
    ]


def public_stream_inventory(config):
    manual = normalize_public_sources(config.get("public_sources", []))
    seen_urls = {source.get("url") for source in manual}
    auto = [source for source in auto_public_sources() if source.get("url") not in seen_urls]
    return [*manual, *auto]


def enabled_source_links(config):
    stream = config.get("stream", {})
    return normalize_links([s.get("url") for s in stream.get("sources", []) if s.get("enabled", True)])


def sync_links_from_sources(stream):
    stream["links"] = normalize_links([s.get("url") for s in stream.get("sources", []) if s.get("enabled", True)])
    return stream["links"]


def normalize_scrape_urls(raw_urls):
    urls = []
    seen = set()
    values = raw_urls if isinstance(raw_urls, list) else [raw_urls]
    for item in values:
        candidate = str(item or "").strip()
        if not valid_stream_url(candidate):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        urls.append(candidate)
    return urls


def normalize_config(config):
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    if not isinstance(config, dict):
        return merged
    for section in ("server", "dashboard", "stream", "arangodb"):
        raw_section = config.get(section, {})
        if not isinstance(raw_section, dict):
            continue
        merged[section].update(raw_section)
    if "public_sources" in config:
        merged["public_sources"] = config.get("public_sources", [])
    merged["public_sources"] = normalize_public_sources(merged.get("public_sources", []))
    stream = merged["stream"]
    stream["sources"] = normalize_sources(stream.get("sources", []), stream.get("links", []))
    stream["links"] = sync_links_from_sources(stream)
    stream["output_dir"] = str(stream.get("output_dir") or DEFAULT_CONFIG["stream"]["output_dir"])
    stream["ffmpeg_log_dir"] = str(stream.get("ffmpeg_log_dir") or DEFAULT_CONFIG["stream"]["ffmpeg_log_dir"])
    stream["scrape_url"] = str(stream.get("scrape_url") or "")
    stream["scrape_urls"] = normalize_scrape_urls(stream.get("scrape_urls", []))
    if valid_stream_url(stream["scrape_url"]) and stream["scrape_url"] not in stream["scrape_urls"]:
        stream["scrape_urls"].insert(0, stream["scrape_url"])
    stream["command"] = str(stream.get("command") or DEFAULT_CONFIG["stream"]["command"])
    stream["encoder"] = str(stream.get("encoder") or DEFAULT_CONFIG["stream"]["encoder"])
    if stream["encoder"] not in ENCODER_CHOICES:
        stream["encoder"] = DEFAULT_CONFIG["stream"]["encoder"]
    stream["bitrate"] = str(stream.get("bitrate") or DEFAULT_CONFIG["stream"]["bitrate"])
    stream["bitrate_720"] = str(stream.get("bitrate_720") or DEFAULT_CONFIG["stream"]["bitrate_720"])
    stream["maxrate_720"] = str(stream.get("maxrate_720") or DEFAULT_CONFIG["stream"]["maxrate_720"])
    stream["bufsize_720"] = str(stream.get("bufsize_720") or DEFAULT_CONFIG["stream"]["bufsize_720"])
    stream["maxrate_1080"] = str(stream.get("maxrate_1080") or DEFAULT_CONFIG["stream"]["maxrate_1080"])
    stream["bufsize_1080"] = str(stream.get("bufsize_1080") or DEFAULT_CONFIG["stream"]["bufsize_1080"])
    stream["audio_bitrate"] = str(stream.get("audio_bitrate") or DEFAULT_CONFIG["stream"]["audio_bitrate"])
    stream["public_dash_url"] = str(stream.get("public_dash_url") or "")
    stream["public_hls_url"] = str(stream.get("public_hls_url") or "")
    stream["include_auto_public_sources"] = bool(stream.get("include_auto_public_sources", True))
    stream["source_manifest_path"] = str(stream.get("source_manifest_path") or DEFAULT_CONFIG["stream"]["source_manifest_path"])
    stream["soursignal_auto_recover"] = bool(stream.get("soursignal_auto_recover", True))
    stream["auto_recover"] = bool(stream.get("auto_recover", True))
    stream["auto_restart_on_exit"] = bool(stream.get("auto_restart_on_exit", True))
    stream["watchdog_restart_cooldown"] = safe_number(stream.get("watchdog_restart_cooldown"), 20, minimum=5)
    stream["startup_grace_seconds"] = safe_number(stream.get("startup_grace_seconds"), 25, minimum=5)
    stream["playlist_stale_seconds"] = safe_number(stream.get("playlist_stale_seconds"), 25, minimum=10)
    stream["min_assessment_seconds"] = safe_number(stream.get("min_assessment_seconds"), 15, minimum=15)
    stream["health_sample_interval"] = safe_number(stream.get("health_sample_interval"), 2, minimum=1)
    stream["success_score_threshold"] = safe_number(stream.get("success_score_threshold"), 180)
    stream["failure_score_threshold"] = safe_number(stream.get("failure_score_threshold"), -120)
    stream["confirmed_failure_samples"] = safe_int(stream.get("confirmed_failure_samples"), 2, minimum=1)
    stream["failure_ramp_seconds"] = safe_number(stream.get("failure_ramp_seconds"), 60, minimum=15)
    arango = merged["arangodb"]
    arango["enabled"] = bool(arango.get("enabled", True))
    arango["url"] = str(arango.get("url") or DEFAULT_CONFIG["arangodb"]["url"])
    arango["database"] = str(arango.get("database") or DEFAULT_CONFIG["arangodb"]["database"])
    arango["username"] = str(arango.get("username") or DEFAULT_CONFIG["arangodb"]["username"])
    arango["password"] = str(arango.get("password") or "")
    dashboard = merged["dashboard"]
    dashboard["password"] = str(dashboard.get("password") or "")
    dashboard["session_token"] = str(dashboard.get("session_token") or "")
    return merged


def current_auto_sources():
    return normalize_links(list(_AUTO_SOURCES))


def effective_stream_links(config):
    stream = config.setdefault("stream", {})
    return enabled_source_links(config) or normalize_links(stream.get("links", []))


def load_config(fresh=False):
    global _CONFIG_CACHE
    now = time.monotonic()
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        mtime = 0.0
    cached = _CONFIG_CACHE
    if (
        not fresh
        and cached["config"] is not None
        and cached["mtime"] == mtime
        and now - cached["at"] < _CONFIG_CACHE_TTL
    ):
        return cached["config"]
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            config = normalize_config(yaml.safe_load(f) or {})
    except FileNotFoundError:
        ERRORS.append({"ts": now_ms(), "level": "error", "line": f"config missing: {CONFIG_PATH}"})
        config = normalize_config({})
    except (yaml.YAMLError, OSError) as exc:
        ERRORS.append({"ts": now_ms(), "level": "error", "line": f"config load failed: {exc}"})
        config = normalize_config({})
    _CONFIG_CACHE = {"config": config, "mtime": mtime, "at": now}
    return config


def save_config(config):
    tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
    normalized = normalize_config(config)
    with tmp.open("w", encoding="utf-8") as f:
        yaml.safe_dump(normalized, f, sort_keys=False)
    os.replace(tmp, CONFIG_PATH)
    # Replace the cache object so the next load_config() sees the new values even
    # if load_config() has already run and reassigned _CONFIG_CACHE to a fresh dict.
    global _CONFIG_CACHE
    _CONFIG_CACHE = {"config": None, "mtime": 0.0, "at": 0.0}


def public_config(config):
    safe = json.loads(json.dumps(config))
    safe.get("dashboard", {}).pop("password", None)
    safe.get("dashboard", {}).pop("session_token", None)
    safe.get("arangodb", {}).pop("password", None)
    for source in safe.get("stream", {}).get("sources", []) or []:
        source.pop("headers", None)
    safe["public_sources"] = [proxied_public_source(source) for source in public_stream_inventory(config)]
    return safe


def public_cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, HEAD, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Cache-Control": "no-cache",
    }


def source_manifest(config):
    return {
        "sources": [
            {
                "url": source.get("url"),
                "headers": source.get("headers") or {},
            }
            for source in config.get("stream", {}).get("sources", [])
            if source.get("enabled", True) and source.get("url")
        ]
    }


def write_source_manifest(config):
    path = Path(config.get("stream", {}).get("source_manifest_path") or DEFAULT_CONFIG["stream"]["source_manifest_path"])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(source_manifest(config), indent=2), encoding="utf-8")
        return str(path)
    except OSError as exc:
        ERRORS.append({"ts": now_ms(), "level": "error", "line": f"source manifest write failed: {exc}"})
        return None


def public_source_status(source, index=0, proc=None):
    source_id = source.get("id") or f"source-{index + 1}"
    health = SOURCE_HEALTH.get(source_id, {})
    return {
        "id": source_id,
        "label": source.get("label") or f"Source {index + 1}",
        "type": source.get("type") or source_type_for_url(None, source.get("url")),
        "url": source.get("url"),
        "enabled": bool(source.get("enabled", True)),
        "preferred": index == 0,
        "in_process": bool(proc and proc.get("managed") and index == 0),
        "health": health.get("state") or ("green" if index == 0 and proc and proc.get("managed") else "unknown"),
        "health_message": health.get("message") or "",
        "checked_at": health.get("checked_at"),
        "viewer_count": viewer_counts_snapshot().get("by_source", {}).get(source_id, 0),
    }


def source_statuses(config, proc=None):
    return [
        public_source_status(source, index=index, proc=proc)
        for index, source in enumerate(config.get("stream", {}).get("sources", []))
    ]


def public_managed_sources(config, proc=None):
    public_hls = config.get("stream", {}).get("public_hls_url") or "/hls/ufc.m3u8"
    return [
        {
            "id": "server-1",
            "label": "Server 1 / Default",
            "type": "managed-hls",
            "url": public_hls,
            "playback_url": "/hls/ufc.m3u8",
            "enabled": True,
            "preferred": True,
            "in_process": bool(proc and proc.get("managed")),
            "state": "preferred",
            "health": "green" if proc and proc.get("managed") else "unknown",
            "viewer_count": viewer_counts_snapshot().get("by_source", {}).get("server-1", 0),
        }
    ]


def prune_viewer_sessions(now=None):
    now = now or time.time()
    expired = [sid for sid, session in VIEWER_SESSIONS.items() if now - float(session.get("at") or 0) > VIEWER_SESSION_TTL]
    for sid in expired:
        VIEWER_SESSIONS.pop(sid, None)


def viewer_counts_snapshot():
    now = time.time()
    expired = [sid for sid, session in VIEWER_SESSIONS.items() if now - float(session.get("at") or 0) > VIEWER_SESSION_TTL]
    for sid in expired:
        VIEWER_SESSIONS.pop(sid, None)
    counts = {"total": 0, "ttl_seconds": VIEWER_SESSION_TTL, "by_source": {}, "sources": [], "updated_at": now_ms()}
    for session in VIEWER_SESSIONS.values():
        source_id = str(session.get("source_id") or "server-1")
        counts["total"] += 1
        counts["by_source"][source_id] = counts["by_source"].get(source_id, 0) + 1
    counts["sources"] = [
        {"id": source_id, "label": str(source_id), "viewer_count": viewer_count}
        for source_id, viewer_count in sorted(counts["by_source"].items())
    ]
    return counts


@dataclass
class StreamHealthScorer:
    pid: int | None = None
    started_at: int | None = None
    last_sample_at: float = 0.0
    consecutive_bad_samples: int = 0
    consecutive_good_samples: int = 0
    previous_hls: dict = field(default_factory=dict)
    samples: deque[dict] = field(default_factory=lambda: deque(maxlen=90))
    last_assessment: dict | None = None

    def reset(self, pid=None, started_at=None):
        self.pid = pid
        self.started_at = started_at
        self.last_sample_at = 0.0
        self.consecutive_bad_samples = 0
        self.consecutive_good_samples = 0
        self.previous_hls = {}
        self.samples.clear()
        self.last_assessment = None

    def assess(self, config, proc, hls, force=False):
        stream = config.get("stream", {})
        recent_errors = recent_stream_errors(limit=8, seconds=60)
        if not proc.get("managed"):
            self.reset()
            assessment = {
                "state": "stopped",
                "level": "warn",
                "decision": "stopped",
                "message": "No managed stream process is running.",
                "score": 0.0,
                "confidence": 100,
                "assessment_elapsed": 0.0,
                "assessment_remaining": float(stream.get("min_assessment_seconds", 15)),
                "evidence": {"recent_error_count": len(recent_errors)},
                "samples": [],
                "recent_errors": recent_errors,
            }
            self.last_assessment = assessment
            return assessment

        pid = proc.get("pid")
        started_at = proc.get("started_at")
        if self.pid != pid or self.started_at != started_at:
            self.reset(pid=pid, started_at=started_at)

        now = time.monotonic()
        sample_interval = float(stream.get("health_sample_interval", 2))
        if self.last_assessment and not force and self.last_sample_at and now - self.last_sample_at < sample_interval:
            return self.last_assessment

        elapsed = float(proc.get("age") or 0.0)
        min_assessment = float(stream.get("min_assessment_seconds", 15))
        stale_seconds = float(stream.get("playlist_stale_seconds", 25))
        ramp_seconds = float(stream.get("failure_ramp_seconds", 60))
        success_threshold = float(stream.get("success_score_threshold", 180))
        failure_threshold = float(stream.get("failure_score_threshold", -120))
        confirmed_failure_samples = int(stream.get("confirmed_failure_samples", 2))

        score, evidence, reasons = score_stream_snapshot(proc, hls, self.previous_hls, elapsed, min_assessment, stale_seconds, ramp_seconds, recent_errors)
        bad_sample = elapsed >= min_assessment and score <= failure_threshold
        good_sample = score >= success_threshold
        if bad_sample:
            self.consecutive_bad_samples += 1
            self.consecutive_good_samples = 0
        elif good_sample:
            self.consecutive_good_samples += 1
            self.consecutive_bad_samples = 0
        elif score > failure_threshold / 2:
            self.consecutive_bad_samples = 0

        if elapsed < min_assessment:
            state = "assessing"
            level = "warn"
            decision = "assessing"
            message = f"Collecting stream evidence for {min_assessment - elapsed:.1f}s before making a failure decision."
        elif bad_sample and self.consecutive_bad_samples >= confirmed_failure_samples:
            state = "failed"
            level = "bad"
            decision = "failed"
            reason_text = "; ".join(reasons[:3]) if reasons else "score remained below failure threshold"
            message = f"Confirmed weak stream after {self.consecutive_bad_samples} bad samples: {reason_text}."
        elif good_sample:
            state = "healthy"
            level = "ok"
            decision = "healthy"
            message = "Stream is producing fresh HLS output with positive progress evidence."
        elif score < 0:
            state = "degraded"
            level = "warn"
            decision = "degraded"
            reason_text = "; ".join(reasons[:3]) if reasons else "score is below zero"
            message = f"Stream is being watched closely: {reason_text}."
        else:
            state = "recovering"
            level = "warn"
            decision = "recovering"
            message = "Stream has some positive evidence, but not enough yet for a healthy decision."

        confidence = confidence_for_assessment(score, elapsed, min_assessment, len(self.samples), self.consecutive_bad_samples, self.consecutive_good_samples)
        sample = {
            "ts": now_ms(),
            "score": round(score, 1),
            "decision": decision,
            "playlist_age": hls.get("playlist_age"),
            "segments": hls.get("segments"),
            "bytes": hls.get("bytes"),
            "bytes_delta": evidence.get("bytes_delta", 0),
            "media_sequence": hls.get("media_sequence"),
            "segment_delta": evidence.get("segment_delta", 0),
            "playlist_moved": evidence.get("playlist_moved", False),
            "recent_error_count": evidence.get("recent_error_count", 0),
        }
        self.samples.append(sample)
        self.last_sample_at = now
        self.previous_hls = {
            "segments": hls.get("segments"),
            "bytes": hls.get("bytes"),
            "playlist_modified_at": hls.get("playlist_modified_at"),
            "media_sequence": hls.get("media_sequence"),
            "last_segment": hls.get("last_segment"),
            "last_segment_size": hls.get("last_segment_size"),
        }
        assessment = {
            "state": state,
            "level": level,
            "decision": decision,
            "message": message,
            "score": round(score, 1),
            "confidence": confidence,
            "assessment_elapsed": round(elapsed, 1),
            "assessment_remaining": round(max(0.0, min_assessment - elapsed), 1),
            "consecutive_bad_samples": self.consecutive_bad_samples,
            "consecutive_good_samples": self.consecutive_good_samples,
            "evidence": evidence,
            "samples": list(self.samples)[-12:],
            "recent_errors": recent_errors,
        }
        self.last_assessment = assessment
        return assessment


STREAM_HEALTH_SCORER = StreamHealthScorer()


def recent_stream_errors(limit=5, seconds=30):
    cutoff = time.time() - seconds
    return [item for item in list(ERRORS) if item.get("ts", 0) / 1000 >= cutoff][-limit:]


def bounded_penalty(base, cap, ramp):
    return min(cap, base * ramp)


def score_stream_snapshot(proc, hls, previous_hls, elapsed, min_assessment, stale_seconds, ramp_seconds, recent_errors):
    score = 0.0
    reasons = []
    has_child = bool(proc.get("children"))
    playlist_exists = bool(hls.get("playlist_exists"))
    playlist_ready = bool(hls.get("playlist_ready"))
    playlist_age = hls.get("playlist_age")
    playlist_fresh = playlist_age is not None and playlist_age <= stale_seconds
    ramp = max(0.15, min(2.5, elapsed / max(ramp_seconds, 1.0)))
    if elapsed < min_assessment:
        ramp *= 0.35

    current_segments = int(hls.get("segments") or 0)
    previous_segments = int(previous_hls.get("segments") or 0) if previous_hls else 0
    segment_delta = max(0, current_segments - previous_segments) if previous_hls else 0
    current_bytes = int(hls.get("bytes") or 0)
    previous_bytes = int(previous_hls.get("bytes") or 0) if previous_hls else 0
    bytes_delta = max(0, current_bytes - previous_bytes) if previous_hls else 0
    playlist_moved = bool(previous_hls and hls.get("playlist_modified_at") and hls.get("playlist_modified_at") != previous_hls.get("playlist_modified_at"))

    media_sequence = safe_float_or_none(hls.get("media_sequence"))
    previous_media_sequence = safe_float_or_none(previous_hls.get("media_sequence")) if previous_hls else None
    media_sequence_advanced = media_sequence is not None and previous_media_sequence is not None and media_sequence > previous_media_sequence
    progress_seen = segment_delta > 0 or bytes_delta > 0 or playlist_moved or media_sequence_advanced

    if proc.get("managed"):
        score += 20
    if has_child:
        score += 20
    else:
        penalty = bounded_penalty(100 + elapsed, 220, ramp)
        score -= penalty
        reasons.append("runner has no ffmpeg child")
    if playlist_exists:
        score += 10
    if playlist_ready:
        score += 35
    else:
        penalty = bounded_penalty(80 + elapsed * 2, 240, ramp)
        score -= penalty
        reasons.append("playlist is not ready")
    if playlist_fresh:
        score += 60
    elif playlist_age is not None:
        stale_over = max(0.0, playlist_age - stale_seconds)
        penalty = bounded_penalty(60 + stale_over * 6, 260, ramp)
        score -= penalty
        reasons.append(f"playlist is stale ({playlist_age:.1f}s old)")
    if media_sequence_advanced:
        score += 60
    if segment_delta > 0:
        score += 45
    if bytes_delta > 0:
        score += 60
    if playlist_moved:
        score += 35
    if hls.get("last_segment_size"):
        score += 25
    if elapsed >= min_assessment:
        score += 40
    no_progress_grace = min(stale_seconds, max(8.0, min_assessment / 2))
    if previous_hls and elapsed >= min_assessment and not progress_seen and (playlist_age is None or playlist_age > no_progress_grace):
        score -= bounded_penalty(30 + elapsed * 1.2, 160, ramp)
        reasons.append("no HLS progress since previous sample")
    if recent_errors:
        score -= min(140, len(recent_errors) * 14 * ramp)
        reasons.append(f"{len(recent_errors)} recent ffmpeg error(s)")
    else:
        score += 15

    evidence = {
        "has_child": has_child,
        "playlist_exists": playlist_exists,
        "playlist_ready": playlist_ready,
        "playlist_fresh": playlist_fresh,
        "playlist_age": playlist_age,
        "segment_delta": segment_delta,
        "bytes_delta": bytes_delta,
        "playlist_moved": playlist_moved,
        "media_sequence_advanced": media_sequence_advanced,
        "progress_seen": progress_seen,
        "recent_error_count": len(recent_errors),
        "ramp": round(ramp, 3),
        "reasons": reasons,
    }
    return score, evidence, reasons


def confidence_for_assessment(score, elapsed, min_assessment, sample_count, bad_samples, good_samples):
    elapsed_score = min(45, (elapsed / max(min_assessment, 1.0)) * 45)
    sample_score = min(30, sample_count * 5)
    signal_score = min(25, abs(score) / 8)
    streak_score = min(15, max(bad_samples, good_samples) * 5)
    confidence = int(min(100, elapsed_score + sample_score + signal_score + streak_score))
    if elapsed < min_assessment:
        return min(85, confidence)
    return confidence


def event(message, level="info", extra=None):
    item = {"ts": now_ms(), "level": level, "message": message, "extra": extra or {}}
    EVENTS.append(item)
    queue_arango_insert("events", item)
    return item


def require_auth(request):
    config = load_config()
    token = config.get("dashboard", {}).get("session_token", "")
    if not token:
        return True
    supplied = request.headers.get("x-obbystreams-token", "") or request.cookies.get("obbystreams_token", "")
    if not supplied:
        return False
    return secrets.compare_digest(supplied, token)


def guarded(handler):
    async def wrapped(request):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            has_header_token = bool(request.headers.get("x-obbystreams-token", "").strip())
            if not trusted_request_origin(request) and not has_header_token:
                return JSONResponse({"ok": False, "error": "forbidden origin"}, status_code=403)
        if not require_auth(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        return await handler(request)
    return wrapped


async def parse_json_body(request):
    if request.headers.get("content-length", "0") == "0":
        return {}
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON body") from exc
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


async def login(request):
    if not trusted_request_origin(request):
        return JSONResponse({"ok": False, "error": "forbidden origin"}, status_code=403)
    config = load_config()
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if body.get("password") != config.get("dashboard", {}).get("password"):
        return JSONResponse({"ok": False, "error": "bad password"}, status_code=401)
    token = config.get("dashboard", {}).get("session_token", "")
    response = JSONResponse({"ok": True})
    secure_cookie = request.url.scheme == "https"
    response.set_cookie("obbystreams_token", token, httponly=True, secure=secure_cookie, samesite="strict", max_age=60 * 60 * 24 * 30)
    return response


def arango_auth_header(config):
    arango = config.get("arangodb", {})
    raw = f"{arango.get('username')}:{arango.get('password')}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


async def arango_request(method, path, payload=None):
    config = load_config()
    arango = config.get("arangodb", {})
    if not arango.get("enabled", True):
        return None
    base = arango.get("url", "http://127.0.0.1:8529").rstrip("/")
    db = arango.get("database", "obbystreams")
    url = f"{base}/_db/{db}{path}"
    headers = {"Content-Type": "application/json", **arango_auth_header(config)}
    timeout = httpx.Timeout(2.5)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(method, url, headers=headers, json=payload)
        response.raise_for_status()
        if not response.content:
            return {"ok": True}
        return response.json()


async def arango_insert(collection, doc):
    try:
        return await arango_request("POST", f"/_api/document/{collection}", doc)
    except Exception:
        return None


def queue_arango_insert(collection, doc):
    global ARANGO_QUEUE
    if ARANGO_QUEUE is None:
        return
    item = {"collection": collection, "doc": doc, "attempt": 1}
    try:
        ARANGO_QUEUE.put_nowait(item)
    except asyncio.QueueFull:
        RUNTIME["arango_dropped_writes"] += 1


async def arango_worker_loop():
    global ARANGO_QUEUE
    while True:
        try:
            if ARANGO_QUEUE is None:
                await asyncio.sleep(0.25)
                continue
            item = await ARANGO_QUEUE.get()
            collection = item.get("collection")
            doc = item.get("doc")
            attempt = int(item.get("attempt", 1))
            if not collection:
                ARANGO_QUEUE.task_done()
                continue
            try:
                await arango_request("POST", f"/_api/document/{collection}", doc)
            except Exception as exc:
                if attempt < ARANGO_RETRY_MAX_ATTEMPTS:
                    retry = {"collection": collection, "doc": doc, "attempt": attempt + 1}
                    delay = 0.25 * (2 ** (attempt - 1))
                    await asyncio.sleep(delay)
                    with contextlib.suppress(asyncio.QueueFull):
                        ARANGO_QUEUE.put_nowait(retry)
                else:
                    RUNTIME["arango_write_failures"] += 1
                    ERRORS.append(
                        {
                            "ts": now_ms(),
                            "level": "error",
                            "line": f"arango insert failed ({collection}): {exc}",
                        }
                    )
            finally:
                ARANGO_QUEUE.task_done()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            ERRORS.append({"ts": now_ms(), "level": "error", "line": f"arango worker error: {exc}"})
            await asyncio.sleep(0.5)


async def arango_status(request):
    try:
        data = await arango_request("GET", "/_api/version")
        return JSONResponse({"ok": True, "connected": True, "version": data})
    except Exception as exc:
        return JSONResponse({"ok": True, "connected": False, "error": str(exc)})


def stream_processes():
    found = []
    current_pid = os.getpid()
    excluded = {current_pid}
    if PROCESS and PROCESS.poll() is None:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            managed = psutil.Process(PROCESS.pid)
            excluded.add(managed.pid)
            excluded.update(child.pid for child in managed.children(recursive=True))
    for proc in psutil.process_iter(["pid", "cmdline", "create_time", "name"]):
        try:
            if proc.info["pid"] in excluded:
                continue
            cmdline = proc.info.get("cmdline") or []
            cmd = " ".join(cmdline)
            base = os.path.basename(cmdline[0]) if cmdline else ""
            if base in {"bwrap", "zsh", "bash", "sh", "timeout", "rg", "grep", "curl"}:
                continue
            if (
                "/usr/bin/obbystreams" in cmdline
                or "/usr/bin/obbystreams" in cmd
                or "/home/joey/obbystreams/bin/obbystreams" in cmdline
                or "/home/joey/obbystreams/bin/obbystreams" in cmd
                or "/usr/bin/ufc" in cmdline
                or "/usr/bin/ufc" in cmd
                or "ufc_tool.py" in cmd
                or "streamUFC" in cmd
            ):
                found.append({
                    "pid": proc.info["pid"],
                    "cmd": cmd,
                    "age": max(0, time.time() - proc.info.get("create_time", time.time())),
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return found


def kill_existing_streams():
    killed = []
    for item in stream_processes():
        try:
            proc = psutil.Process(item["pid"])
            proc.terminate()
            killed.append(item)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    procs = []
    for item in killed:
        try:
            procs.append(psutil.Process(item["pid"]))
        except psutil.NoSuchProcess:
            continue
    gone, alive = psutil.wait_procs(procs, timeout=2)
    for proc in alive:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.kill()
    return killed


def should_watchdog_restart_exited_process(config, desired_state):
    stream = config.get("stream", {})
    return (
        desired_state == "running"
        and stream.get("auto_recover", True)
        and stream.get("auto_restart_on_exit", True)
        and bool(effective_stream_links(config))
    )


def safe_stat_size(path):
    try:
        return path.stat().st_size
    except OSError:
        return 0


def safe_stat_mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def classify_stream_log(line):
    lowered = line.lower()
    if "starting" in lowered or "stream commander" in lowered or "status:" in lowered:
        return "info"
    if "ffmpeg:" in lowered or any(token in lowered for token in ("error", "failed", "invalid", "timed out", "timeout", "403", "404", "500")):
        return "error"
    if "ffmpeg exited" in lowered or "restart" in lowered or "weak stream" in lowered or "every link failed" in lowered:
        return "warn"
    return "debug"


def _read_playlist(path):
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _parse_playlist_lines(lines):
    media_sequence = None
    target_duration = None
    segment_names = []
    segment_durations = []
    media_playlists = []
    for line in lines:
        if line.startswith("#EXT-X-TARGETDURATION:"):
            target_duration = line.split(":", 1)[1]
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            media_sequence = line.split(":", 1)[1]
        elif line.startswith("#EXTINF:"):
            with contextlib.suppress(ValueError):
                segment_durations.append(float(line.split(":", 1)[1].split(",", 1)[0]))
        elif line and not line.startswith("#"):
            if line.endswith(".m3u8"):
                media_playlists.append(line)
            else:
                segment_names.append(line)
    return media_sequence, target_duration, segment_names, segment_durations, media_playlists


def hls_metrics(config):
    stream = config.get("stream", {})
    output_dir = Path(stream.get("output_dir", "/var/www/live.obnoxious.lol/stream"))
    playlist = output_dir / "ufc.m3u8"
    dash_manifest = output_dir / "ufc.mpd"
    media_playlist_paths = [Path(p) for p in glob.glob(str(output_dir / "media_*.m3u8"))]
    segments = [
        Path(p)
        for pattern in ("ufc*.ts", "ufc*.m4s", "ufc*.mp4")
        for p in glob.glob(str(output_dir / pattern))
    ]
    total_bytes = sum(safe_stat_size(p) for p in segments)
    playlist_age = None
    playlist_mtime = None
    dash_manifest_age = None
    dash_manifest_mtime = None
    playlist_lines = _read_playlist(playlist)
    target_duration = None
    media_sequence = None
    playlist_segment_names = []
    segment_durations = []
    segment_mtimes = [safe_stat_mtime(p) for p in segments]
    segment_mtimes = [m for m in segment_mtimes if m is not None]
    if playlist.exists():
        playlist_mtime = playlist.stat().st_mtime
        playlist_age = max(0, time.time() - playlist_mtime)
    if dash_manifest.exists():
        dash_manifest_mtime = dash_manifest.stat().st_mtime
        dash_manifest_age = max(0, time.time() - dash_manifest_mtime)
    media_sequence, target_duration, playlist_segment_names, segment_durations, media_playlist_names = _parse_playlist_lines(playlist_lines)
    if not playlist_segment_names:
        candidate_paths = []
        for name in media_playlist_names:
            candidate_paths.append(output_dir / name)
        candidate_paths.extend(media_playlist_paths)
        seen = set()
        for media_playlist in candidate_paths:
            if media_playlist in seen:
                continue
            seen.add(media_playlist)
            parsed_sequence, parsed_target, parsed_segments, parsed_durations, _ = _parse_playlist_lines(_read_playlist(media_playlist))
            media_sequence = parsed_sequence or media_sequence
            target_duration = parsed_target or target_duration
            playlist_segment_names.extend(parsed_segments)
            segment_durations.extend(parsed_durations)
    return {
        "output_dir": str(output_dir),
        "playlist": str(playlist),
        "dash_manifest": str(dash_manifest),
        "playlist_exists": playlist.exists(),
        "dash_manifest_exists": dash_manifest.exists(),
        "playlist_ready": bool(playlist_segment_names),
        "playlist_age": playlist_age,
        "dash_manifest_age": dash_manifest_age,
        "playlist_modified_at": int(playlist_mtime * 1000) if playlist_mtime else None,
        "dash_manifest_modified_at": int(dash_manifest_mtime * 1000) if dash_manifest_mtime else None,
        "playlist_line_count": len(playlist_lines),
        "media_playlists": [p.name for p in media_playlist_paths],
        "segments": len(segments),
        "bytes": total_bytes,
        "latest_segment_modified_at": int(max(segment_mtimes) * 1000) if segment_mtimes else None,
        "oldest_segment_modified_at": int(min(segment_mtimes) * 1000) if segment_mtimes else None,
        "target_duration": target_duration,
        "media_sequence": media_sequence,
        "segment_window_seconds": round(sum(segment_durations), 3),
        "playlist_segment_count": len(playlist_segment_names),
        "playlist_segments": playlist_segment_names[-12:],
        "first_segment": playlist_segment_names[0] if playlist_segment_names else None,
        "last_segment": playlist_segment_names[-1] if playlist_segment_names else None,
        "last_segment_size": safe_stat_size(output_dir / playlist_segment_names[-1]) if playlist_segment_names else None,
        "public_dash_url": stream.get("public_dash_url"),
        "public_hls_url": stream.get("public_hls_url"),
        "dashboard_dash_url": "/hls/ufc.mpd",
        "dashboard_hls_url": "/hls/ufc.m3u8",
    }


def process_metrics():
    global PROCESS, STARTED_AT
    pid = PROCESS.pid if PROCESS and PROCESS.poll() is None else None
    data = {"managed": bool(pid), "pid": pid, "started_at": STARTED_AT, "age": None, "cpu": None, "rss": None, "children": []}
    if not pid:
        return data
    try:
        proc = psutil.Process(pid)
        data["age"] = max(0, time.time() - proc.create_time())
        data["cpu"] = proc.cpu_percent(interval=0.0)
        data["rss"] = proc.memory_info().rss
        data["cmd"] = " ".join(proc.cmdline())
        data["children"] = [
            {"pid": c.pid, "name": c.name(), "cpu": c.cpu_percent(interval=0.0), "rss": c.memory_info().rss}
            for c in proc.children(recursive=True)
        ]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return data


def stream_health(config, proc, hls, force=False):
    return STREAM_HEALTH_SCORER.assess(config, proc, hls, force=force)


NVIDIA_GPU_QUERY_FIELDS = [
    "index",
    "name",
    "uuid",
    "driver_version",
    "pstate",
    "temperature.gpu",
    "utilization.gpu",
    "utilization.memory",
    "memory.total",
    "memory.used",
    "memory.free",
    "power.draw",
    "power.limit",
    "clocks.current.graphics",
    "clocks.current.memory",
]
NVIDIA_GPU_FIELDS = [
    "index",
    "name",
    "uuid",
    "driver_version",
    "pstate",
    "temperature_gpu",
    "utilization_gpu",
    "utilization_memory",
    "memory_total",
    "memory_used",
    "memory_free",
    "power_draw",
    "power_limit",
    "clocks_graphics",
    "clocks_memory",
]
NVIDIA_ENCODER_QUERY_FIELDS = [
    "index",
    "encoder.stats.sessionCount",
    "encoder.stats.averageFps",
    "encoder.stats.averageLatency",
]
NVIDIA_ENCODER_FIELDS = [
    "index",
    "encoder_session_count",
    "encoder_average_fps",
    "encoder_average_latency_ms",
]
NVIDIA_PROCESS_QUERY_FIELDS = ["gpu_uuid", "pid", "process_name", "used_memory"]


def parse_smi_csv(text, fields):
    rows = []
    reader = csv.reader(io.StringIO(text or ""))
    for raw in reader:
        if not any(cell.strip() for cell in raw):
            continue
        padded = (raw + [""] * len(fields))[: len(fields)]
        rows.append({field: cell.strip() for field, cell in zip(fields, padded, strict=False)})
    return rows


def parse_nvidia_gpu_csv(text):
    gpus = []
    for row in parse_smi_csv(text, NVIDIA_GPU_FIELDS):
        total = smi_int(row.get("memory_total"))
        used = smi_int(row.get("memory_used"))
        power_draw = smi_float(row.get("power_draw"))
        power_limit = smi_float(row.get("power_limit"))
        gpu = {
            "index": smi_int(row.get("index")),
            "name": smi_text(row.get("name")),
            "uuid": smi_text(row.get("uuid")),
            "driver_version": smi_text(row.get("driver_version")),
            "pstate": smi_text(row.get("pstate")),
            "temperature_c": smi_int(row.get("temperature_gpu")),
            "gpu_utilization_pct": smi_int(row.get("utilization_gpu")),
            "memory_utilization_pct": smi_int(row.get("utilization_memory")),
            "memory_total_mb": total,
            "memory_used_mb": used,
            "memory_free_mb": smi_int(row.get("memory_free")),
            "memory_used_pct": smi_percent(used, total),
            "power_draw_w": power_draw,
            "power_limit_w": power_limit,
            "power_used_pct": smi_percent(power_draw, power_limit),
            "graphics_clock_mhz": smi_int(row.get("clocks_graphics")),
            "memory_clock_mhz": smi_int(row.get("clocks_memory")),
            "encoder_session_count": None,
            "encoder_average_fps": None,
            "encoder_average_latency_ms": None,
        }
        gpus.append(gpu)
    return gpus


def parse_nvidia_encoder_csv(text):
    rows = []
    for row in parse_smi_csv(text, NVIDIA_ENCODER_FIELDS):
        rows.append(
            {
                "index": smi_int(row.get("index")),
                "encoder_session_count": smi_int(row.get("encoder_session_count")),
                "encoder_average_fps": smi_int(row.get("encoder_average_fps")),
                "encoder_average_latency_ms": smi_int(row.get("encoder_average_latency_ms")),
            }
        )
    return rows


def parse_nvidia_process_csv(text):
    processes = []
    for row in parse_smi_csv(text, NVIDIA_PROCESS_QUERY_FIELDS):
        pid = smi_int(row.get("pid"))
        if pid is None:
            continue
        processes.append(
            {
                "gpu_uuid": smi_text(row.get("gpu_uuid")),
                "pid": pid,
                "process_name": smi_text(row.get("process_name")),
                "used_memory_mb": smi_int(row.get("used_memory")),
            }
        )
    return processes


def parse_nvidia_pmon(text):
    rows = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 7:
            continue
        gpu_index = smi_int(parts[0])
        pid = smi_int(parts[1])
        if gpu_index is None or pid is None:
            continue
        command = parts[-1] if len(parts) >= 8 else None
        rows.append(
            {
                "gpu_index": gpu_index,
                "pid": pid,
                "type": smi_text(parts[2]),
                "sm_pct": smi_int(parts[3]),
                "mem_pct": smi_int(parts[4]),
                "enc_pct": smi_int(parts[5]),
                "dec_pct": smi_int(parts[6]),
                "process_name": smi_text(command),
            }
        )
    return rows


def merge_nvidia_processes(compute_processes, pmon_processes, gpus):
    uuid_to_index = {gpu.get("uuid"): gpu.get("index") for gpu in gpus if gpu.get("uuid")}
    merged = {}
    for proc in compute_processes:
        key = (proc.get("gpu_uuid"), proc.get("pid"))
        item = dict(proc)
        item["gpu_index"] = uuid_to_index.get(proc.get("gpu_uuid"))
        merged[key] = item
    for proc in pmon_processes:
        item = next((candidate for candidate in merged.values() if candidate.get("pid") == proc.get("pid")), None)
        if item is None:
            key = (proc.get("gpu_index"), proc.get("pid"))
            item = merged.setdefault(key, {"pid": proc.get("pid"), "gpu_index": proc.get("gpu_index")})
        item.update({k: v for k, v in proc.items() if v is not None})
    for item in merged.values():
        name = str(item.get("process_name") or "").lower()
        item["is_ffmpeg"] = "ffmpeg" in name
    return sorted(merged.values(), key=lambda item: (item.get("gpu_index") is None, item.get("gpu_index") or -1, item.get("pid") or -1))


def run_nvidia_smi(args, timeout=3.5):
    cmd = ["nvidia-smi", *args]
    started = time.monotonic()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {
            "command": cmd,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        }
    except FileNotFoundError as exc:
        return {
            "command": cmd,
            "returncode": 127,
            "stdout": "",
            "stderr": str(exc),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr or ""
        return {
            "command": cmd,
            "returncode": 124,
            "stdout": stdout,
            "stderr": stderr + f"\ntimed out after {timeout:.1f}s",
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        }


def text_tail(text, max_chars=1200):
    text = str(text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def nvidia_command_summary(result, include_stdout=False):
    summary = {
        "command": " ".join(result.get("command", [])),
        "returncode": result.get("returncode"),
        "elapsed_ms": result.get("elapsed_ms"),
        "stderr": text_tail(result.get("stderr"), 900),
    }
    if include_stdout or result.get("returncode"):
        summary["stdout"] = text_tail(result.get("stdout"), 1200)
    return summary


def max_or_none(values):
    filtered = [value for value in values if value is not None]
    return max(filtered) if filtered else None


def sum_or_none(values):
    filtered = [value for value in values if value is not None]
    return round(sum(filtered), 1) if filtered else None


def analyze_nvidia_smi(gpus, processes, commands):
    gpu_command = commands.get("gpus", {})
    available = gpu_command.get("returncode") == 0 and bool(gpus)
    errors = []
    diagnosis = []
    if not available:
        detail = text_tail(gpu_command.get("stderr") or gpu_command.get("stdout") or "nvidia-smi returned no GPU rows", 500)
        errors.append(detail)
        diagnosis.append(detail)
        return {
            "available": False,
            "level": "bad",
            "message": detail or "nvidia-smi is unavailable.",
            "diagnosis": diagnosis,
            "errors": errors,
            "summary": {
                "gpu_count": 0,
                "driver_version": None,
                "max_temperature_c": None,
                "max_gpu_utilization_pct": None,
                "max_memory_used_pct": None,
                "power_draw_w": None,
                "power_limit_w": None,
                "encoder_session_count": 0,
                "encoder_utilization_pct": None,
                "process_count": 0,
                "ffmpeg_process_count": 0,
                "stream_gpu_active": False,
            },
        }

    hot = [gpu for gpu in gpus if (gpu.get("temperature_c") or 0) >= 88]
    memory_high = [gpu for gpu in gpus if (gpu.get("memory_used_pct") or 0) >= 92]
    ffmpeg_processes = [proc for proc in processes if proc.get("is_ffmpeg")]
    encoder_session_count = sum(gpu.get("encoder_session_count") or 0 for gpu in gpus)
    encoder_utilization = max_or_none(proc.get("enc_pct") for proc in processes)
    stream_gpu_active = bool(ffmpeg_processes or encoder_session_count or (encoder_utilization or 0) > 0)

    if hot:
        diagnosis.append(f"{len(hot)} GPU(s) at or above 88C")
    if memory_high:
        diagnosis.append(f"{len(memory_high)} GPU(s) above 92% memory")
    if stream_gpu_active:
        diagnosis.append("FFmpeg/NVENC activity detected")
    else:
        diagnosis.append("No FFmpeg/NVENC process visible to nvidia-smi")

    optional_failures = [
        name
        for name in ("encoder", "processes", "pmon")
        if commands.get(name, {}).get("returncode") not in (None, 0)
    ]
    if optional_failures:
        diagnosis.append(f"Optional query failed: {', '.join(optional_failures)}")

    level = "bad" if hot else "warn" if memory_high else "ok"
    if stream_gpu_active:
        message = "GPU telemetry online. FFmpeg/NVENC activity is visible."
    else:
        message = "GPU telemetry online. No FFmpeg GPU process is visible right now."

    return {
        "available": True,
        "level": level,
        "message": message,
        "diagnosis": diagnosis,
        "errors": errors,
        "summary": {
            "gpu_count": len(gpus),
            "driver_version": next((gpu.get("driver_version") for gpu in gpus if gpu.get("driver_version")), None),
            "max_temperature_c": max_or_none(gpu.get("temperature_c") for gpu in gpus),
            "max_gpu_utilization_pct": max_or_none(gpu.get("gpu_utilization_pct") for gpu in gpus),
            "max_memory_used_pct": max_or_none(gpu.get("memory_used_pct") for gpu in gpus),
            "power_draw_w": sum_or_none(gpu.get("power_draw_w") for gpu in gpus),
            "power_limit_w": sum_or_none(gpu.get("power_limit_w") for gpu in gpus),
            "encoder_session_count": encoder_session_count,
            "encoder_utilization_pct": encoder_utilization,
            "process_count": len(processes),
            "ffmpeg_process_count": len(ffmpeg_processes),
            "stream_gpu_active": stream_gpu_active,
        },
    }


def collect_nvidia_smi():
    checked_at = now_ms()
    gpu_result = run_nvidia_smi(
        [
            f"--query-gpu={','.join(NVIDIA_GPU_QUERY_FIELDS)}",
            "--format=csv,noheader,nounits",
        ]
    )
    gpus = parse_nvidia_gpu_csv(gpu_result.get("stdout", "")) if gpu_result.get("returncode") == 0 else []
    commands = {"gpus": gpu_result}

    if gpus:
        encoder_result = run_nvidia_smi(
            [
                f"--query-gpu={','.join(NVIDIA_ENCODER_QUERY_FIELDS)}",
                "--format=csv,noheader,nounits",
            ]
        )
        commands["encoder"] = encoder_result
        if encoder_result.get("returncode") == 0:
            by_index = {gpu.get("index"): gpu for gpu in gpus}
            for row in parse_nvidia_encoder_csv(encoder_result.get("stdout", "")):
                gpu = by_index.get(row.get("index"))
                if gpu:
                    gpu.update({k: v for k, v in row.items() if k != "index"})

        process_result = run_nvidia_smi(
            [
                f"--query-compute-apps={','.join(NVIDIA_PROCESS_QUERY_FIELDS)}",
                "--format=csv,noheader,nounits",
            ]
        )
        commands["processes"] = process_result
        compute_processes = parse_nvidia_process_csv(process_result.get("stdout", "")) if process_result.get("returncode") == 0 else []

        pmon_result = run_nvidia_smi(["pmon", "-c", "1", "-s", "um"], timeout=4.5)
        commands["pmon"] = pmon_result
        pmon_processes = parse_nvidia_pmon(pmon_result.get("stdout", "")) if pmon_result.get("returncode") == 0 else []
        processes = merge_nvidia_processes(compute_processes, pmon_processes, gpus)
    else:
        processes = []

    analysis = analyze_nvidia_smi(gpus, processes, commands)
    return {
        "ok": True,
        "checked_at": checked_at,
        "collector_interval_seconds": NVIDIA_SMI_CACHE_SECONDS,
        "available": analysis["available"],
        "level": analysis["level"],
        "message": analysis["message"],
        "diagnosis": analysis["diagnosis"],
        "errors": analysis["errors"],
        "summary": analysis["summary"],
        "gpus": gpus,
        "processes": processes,
        "commands": {
            name: nvidia_command_summary(result, include_stdout=(name == "gpus" and result.get("returncode") != 0))
            for name, result in commands.items()
        },
    }


async def nvidia_smi_status(request):
    global NVIDIA_SMI_CACHE
    async with NVIDIA_SMI_LOCK:
        cache_age = time.monotonic() - float(NVIDIA_SMI_CACHE.get("at") or 0.0)
        cached_payload = NVIDIA_SMI_CACHE.get("payload")
        if cached_payload and cache_age < NVIDIA_SMI_CACHE_SECONDS:
            payload = json.loads(json.dumps(cached_payload))
            payload["cached"] = True
            payload["cache_age_seconds"] = round(cache_age, 2)
            return JSONResponse(payload)

        payload = await asyncio.to_thread(collect_nvidia_smi)
        NVIDIA_SMI_CACHE = {"at": time.monotonic(), "payload": payload}
        payload = json.loads(json.dumps(payload))
        payload["cached"] = False
        payload["cache_age_seconds"] = 0.0
        queue_arango_insert("metrics", {"ts": now_ms(), "kind": "nvidia_smi", "payload": payload})
        return JSONResponse(payload)


def status_payload():
    config = load_config()
    proc = process_metrics()
    hls = hls_metrics(config)
    configured_links = normalize_links(config.get("stream", {}).get("links", []))
    auto_links = current_auto_sources()
    active_links = effective_stream_links(config)
    sources = source_statuses(config, proc)
    payload = {
        "ok": True,
        "config": public_config(config),
        "managed_process": proc,
        "existing_processes": stream_processes(),
        "hls": hls,
        "health": stream_health(config, proc, hls),
        "sources": sources,
        "viewers": viewer_counts_snapshot(),
        "events": list(EVENTS)[-80:],
        "logs": list(LOGS)[-140:],
        "errors": list(ERRORS)[-80:],
        "server_time": now_ms(),
        "runtime": {
            **RUNTIME,
            "app_started_at": APP_STARTED_AT,
            "app_uptime_seconds": round(max(0.0, time.time() - (APP_STARTED_AT / 1000)), 2) if APP_STARTED_AT else None,
            "arango_queue_depth": ARANGO_QUEUE.qsize() if ARANGO_QUEUE else 0,
            "stream_desired_state": STREAM_DESIRED_STATE,
            "configured_link_count": len(configured_links),
            "configured_source_count": len(sources),
            "auto_public_source_count": len(auto_links),
            "active_link_pool_count": len(active_links),
            "proxy_cache": _PROXY_CACHE.stats(),
        },
    }
    queue_arango_insert("metrics", {"ts": now_ms(), "payload": payload})
    return payload


async def status(request):
    return JSONResponse(status_payload())


async def list_sources(request):
    config = load_config()
    return JSONResponse({"ok": True, "sources": source_statuses(config, process_metrics()), "viewers": viewer_counts_snapshot()})


async def viewer_counts(request):
    cors = public_cors_headers()
    if request.method == "OPTIONS":
        return Response("", headers=cors)
    if request.method == "POST":
        try:
            body = await parse_json_body(request)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400, headers=cors)
        session_id = str(body.get("session_id") or body.get("viewer_id") or secrets.token_urlsafe(16))
        source_id = str(body.get("source_id") or "server-1")
        async with VIEWER_LOCK:
            prune_viewer_sessions()
            VIEWER_SESSIONS[session_id] = {"source_id": source_id, "at": time.time()}
            counts = viewer_counts_snapshot()
        return JSONResponse({"ok": True, "session_id": session_id, "viewers": counts}, headers=cors)
    async with VIEWER_LOCK:
        counts = viewer_counts_snapshot()
    return JSONResponse({"ok": True, "viewers": counts}, headers=cors)


async def health(request):
    config = load_config()
    proc = process_metrics()
    hls = hls_metrics(config)
    stream = config.get("stream", {})
    links_configured = bool(stream.get("links"))
    playlist_stale_seconds = float(stream.get("playlist_stale_seconds", 25))
    health_doc = stream_health(config, proc, hls, force=True)
    ready = bool(proc.get("managed") and hls.get("playlist_ready"))
    stale = hls.get("playlist_age") is not None and hls.get("playlist_age", 0) > playlist_stale_seconds
    checks = {
        "managed_process": bool(proc.get("managed")),
        "links_configured": links_configured,
        "playlist_ready": bool(hls.get("playlist_ready")),
        "playlist_fresh": not stale,
        "confirmed_failure": health_doc.get("decision") == "failed",
        "assessment_complete": not health_doc.get("assessment_remaining"),
    }
    ok = checks["managed_process"] and checks["links_configured"] and not checks["confirmed_failure"]
    status_code = 200 if ok else 503
    return JSONResponse(
        {
            "ok": ok,
            "ready": ready and not stale and not checks["confirmed_failure"],
            "checks": checks,
            "health": health_doc,
            "server_time": now_ms(),
        },
        status_code=status_code,
    )


async def get_config(request):
    return JSONResponse({"ok": True, "config": public_config(load_config())})


async def put_config(request):
    config = load_config()
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    stream = config.setdefault("stream", {})
    if "public_sources" in body:
        if not isinstance(body["public_sources"], list):
            return JSONResponse({"ok": False, "error": "public_sources must be an array"}, status_code=400)
        config["public_sources"] = normalize_public_sources(body["public_sources"])
    if "links" in body:
        links = body["links"]
        if not isinstance(links, list):
            return JSONResponse({"ok": False, "error": "links must be an array"}, status_code=400)
        stream["sources"] = normalize_sources(stream.get("sources", []), links)
        stream["links"] = sync_links_from_sources(stream)
    if "sources" in body:
        if not isinstance(body["sources"], list):
            return JSONResponse({"ok": False, "error": "sources must be an array"}, status_code=400)
        stream["sources"] = normalize_sources(body["sources"])
        stream["links"] = sync_links_from_sources(stream)
    for key in (
        "encoder",
        "bitrate",
        "bitrate_720",
        "maxrate_720",
        "bufsize_720",
        "maxrate_1080",
        "bufsize_1080",
        "audio_bitrate",
        "include_auto_public_sources",
        "source_manifest_path",
        "soursignal_auto_recover",
        "output_dir",
        "ffmpeg_log_dir",
        "public_dash_url",
        "public_hls_url",
        "auto_recover",
        "auto_restart_on_exit",
        "watchdog_restart_cooldown",
        "startup_grace_seconds",
        "playlist_stale_seconds",
        "min_assessment_seconds",
        "health_sample_interval",
        "success_score_threshold",
        "failure_score_threshold",
        "confirmed_failure_samples",
        "failure_ramp_seconds",
    ):
        if key in body:
            stream[key] = body[key]
    save_config(config)
    event("configuration updated", "ok", {"keys": list(body.keys())})
    queue_arango_insert("configs", {"ts": now_ms(), "config": public_config(config)})
    stream_restart_keys = {
        "links",
        "sources",
        "encoder",
        "bitrate",
        "bitrate_720",
        "maxrate_720",
        "bufsize_720",
        "maxrate_1080",
        "bufsize_1080",
        "audio_bitrate",
        "include_auto_public_sources",
        "source_manifest_path",
        "soursignal_auto_recover",
        "output_dir",
        "ffmpeg_log_dir",
        "public_dash_url",
        "public_hls_url",
        "restart_delay",
        "max_restart_delay",
        "backoff_multiplier",
        "backoff_jitter",
        "rate_limit_delay",
        "quick_fail",
        "stop_after_failed_rounds",
        "min_assessment_seconds",
        "success_score_threshold",
        "failure_score_threshold",
        "confirmed_failure_samples",
        "failure_ramp_seconds",
    }
    restarted = await restart_managed_with_config("configuration changed") if stream_restart_keys.intersection(body) else False
    if restarted:
        event("running stream picked up updated configuration", "ok")
    return JSONResponse({"ok": True, "config": public_config(config)})


async def add_link(request):
    config = load_config()
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    url = str(body.get("url", "")).strip()
    if not url:
        return JSONResponse({"ok": False, "error": "url required"}, status_code=400)
    if not valid_stream_url(url):
        return JSONResponse({"ok": False, "error": "url must be http(s)"}, status_code=400)
    stream = config.setdefault("stream", {})
    sources = normalize_sources(stream.get("sources", []), stream.get("links", []))
    if url not in {source.get("url") for source in sources}:
        sources.append(
            {
                "id": f"source-{len(sources) + 1}",
                "label": str(body.get("label") or f"Source {len(sources) + 1}").strip(),
                "url": url,
                "type": source_type_for_url(body.get("type"), url),
                "enabled": True,
                "headers": normalize_source_headers(body.get("headers")),
            }
        )
    stream["sources"] = normalize_sources(sources)
    sync_links_from_sources(stream)
    save_config(config)
    event("link added", "ok", {"url": url})
    queue_arango_insert("links", {"ts": now_ms(), "action": "add", "url": url})
    restarted = await restart_managed_with_config("link added")
    if restarted:
        event("running stream picked up updated links", "ok")
    return JSONResponse({"ok": True, "links": config["stream"]["links"], "sources": source_statuses(config, process_metrics())})


async def remove_link(request):
    config = load_config()
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    url = str(body.get("url", "")).strip()
    stream = config.setdefault("stream", {})
    sources = normalize_sources(stream.get("sources", []), stream.get("links", []))
    stream["sources"] = [source for source in sources if source.get("url") != url and source.get("id") != body.get("id")]
    sync_links_from_sources(stream)
    save_config(config)
    event("link removed", "warn", {"url": url})
    queue_arango_insert("links", {"ts": now_ms(), "action": "remove", "url": url})
    restarted = await restart_managed_with_config("link removed")
    if restarted:
        event("running stream picked up updated links", "ok")
    return JSONResponse({"ok": True, "links": config["stream"]["links"], "sources": source_statuses(config, process_metrics())})


_SCRAPE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0"
)
_SCRAPE_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
_EMBED_HOSTS = {"gooz.aapmains.net"}
_PLAYLIST_HOST_RE = re.compile(
    r"https?://[a-zA-Z0-9-]+\.hereisman\.net/playlist/\d+/[^\s\"'<>&]+"
)
# Captures embed IDs from: iframe src, changeStream() onclick, and js assignment
_GOOZ_EMBED_RE = re.compile(
    r"(?:gooz\.aapmains\.net/new-stream-embed/|changeStream\()(\d+)"
)
_M3U8_RE = re.compile(r"https?://[^\s\"'<>&]+\.m3u8(?:[^\s\"'<>&]*)?")
_CONST_SOURCE_RE = re.compile(r"""const\s+source\s*=\s*["']([^"']+)["']""")
_OPTION_VALUE_RE = re.compile(r"""<option[^>]+value=["']([^"']+)["']""", re.IGNORECASE)
_MMA_LISTINGS_URL = "https://sportsurge.ws/mma/livestreams3"
_SPORTSURGE_EVENT_RE = re.compile(r'href="(https://sportsurge\.ws/event/[^"]+)"')
_UFC_CONTEXT_RE = re.compile(r"ufc", re.IGNORECASE)


def source_headers_for_url(raw_url):
    try:
        target = urlparse(raw_url)
    except Exception:
        return {}
    config = load_config()
    configured_sources = [
        *config.get("stream", {}).get("sources", []),
        *config.get("public_sources", []),
    ]
    for source in configured_sources:
        source_url = source.get("url")
        headers = source.get("headers") or {}
        if not source_url or not headers:
            continue
        parsed = urlparse(source_url)
        if raw_url == source_url:
            return dict(headers)
        if target.scheme == parsed.scheme and target.netloc == parsed.netloc:
            source_path = parsed.path.rsplit("/", 1)[0].rstrip("/")
            if source_path and target.path.startswith(source_path + "/"):
                return dict(headers)
    return {}


def proxy_request_headers(raw_url):
    headers = {
        "User-Agent": _SCRAPE_UA,
        "Origin": _GOOZ_ORIGIN,
        "Referer": _GOOZ_REFERER,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "DNT": "1",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    }
    headers.update(source_headers_for_url(raw_url))
    return headers


async def _scrape_fetch(url: str, referer: str | None = None) -> str:
    """Fetch a page with browser-like headers. Uses subprocess curl to handle
    brotli/zstd compression that older httpx builds may not support."""
    headers_args: list[str] = [
        "-H", f"User-Agent: {_SCRAPE_UA}",
        "-H", f"Accept: {_SCRAPE_ACCEPT}",
        "-H", "Accept-Language: en-US,en;q=0.5",
        "-H", "DNT: 1",
        "-H", "Sec-GPC: 1",
        "-H", "Upgrade-Insecure-Requests: 1",
        "-H", "Sec-Fetch-Dest: document",
        "-H", "Sec-Fetch-Mode: navigate",
        "-H", "Sec-Fetch-Site: same-origin",
        "-H", "Pragma: no-cache",
        "-H", "Cache-Control: no-cache",
    ]
    if referer:
        headers_args += ["-H", f"Referer: {referer}"]
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "--compressed", "--max-time", "15", "-L", url,
            *headers_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        return stdout.decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("scrape fetch failed %s: %s", url, exc)
        return ""


async def _scrape_gooz_embed(embed_id: str, page_url: str) -> list[str]:
    """Fetch a gooz embed page and return playlist URLs found in it."""
    embed_url = f"https://gooz.aapmains.net/new-stream-embed/{embed_id}"
    # Gooz requires Origin from sportsurge for CORS; use its own origin as referer
    html = await _scrape_fetch(embed_url, referer=page_url)
    found: list[str] = []
    seen: set[str] = set()
    for m in _CONST_SOURCE_RE.finditer(html):
        u = m.group(1).strip()
        if u not in seen and valid_stream_url(u):
            seen.add(u)
            found.append(u)
    for m in _PLAYLIST_HOST_RE.finditer(html):
        u = m.group(0).strip()
        if u not in seen and valid_stream_url(u):
            seen.add(u)
            found.append(u)
    return found


def _decode_base64_url(value: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    padded = text + "=" * (-len(text) % 4)
    try:
        decoded = base64.b64decode(padded).decode("utf-8", errors="replace").strip()
    except Exception:
        return None
    return decoded if valid_stream_url(decoded) else None


def _extract_icelz_option_streams(page_url: str, html_text: str) -> list[str]:
    streams: list[str] = []
    seen: set[str] = set()

    def add(candidate: str | None) -> None:
        url = str(candidate or "").strip()
        if not url or url in seen or not valid_stream_url(url):
            return
        if ".m3u8" not in url.lower():
            return
        seen.add(url)
        streams.append(url)

    for match in _OPTION_VALUE_RE.finditer(html_text):
        raw_value = html.unescape(match.group(1).strip())
        if not raw_value:
            continue
        full_value = urljoin(page_url, raw_value) if raw_value.startswith("/") else raw_value
        parsed = urlparse(full_value)

        if valid_stream_url(full_value) and ".m3u8" in full_value.lower():
            add(full_value)

        query = parse_qs(parsed.query)
        for key in ("hlss", "hls", "m3u8", "playlist"):
            for encoded in query.get(key, []):
                add(_decode_base64_url(unquote(encoded)))

    return streams


async def _validate_hls_candidate(url: str) -> bool:
    if not valid_stream_url(url) or ".m3u8" not in url.lower():
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "--compressed", "--max-time", "10", "-L", "-I",
            "-H", f"User-Agent: {_SCRAPE_UA}",
            "-H", "Accept: application/vnd.apple.mpegurl,*/*;q=0.8",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=12)
    except Exception:
        return False
    header_block = stdout.decode("utf-8", errors="replace").lower()
    if " 200 " not in header_block and " 206 " not in header_block:
        return False
    return "mpegurl" in header_block or "audio/mpegurl" in header_block or url.lower().endswith(".m3u8")


async def _scrape_page(url: str) -> list[str]:
    """Extract hereisman playlist URLs from a SportSurge event page or embed."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()

    if "hereisman.net" in host and "/playlist/" in parsed.path:
        return [url] if valid_stream_url(url) else []

    if host in _EMBED_HOSTS:
        m = re.search(r"/new-stream-embed/(\d+)", parsed.path)
        if m:
            return await _scrape_gooz_embed(m.group(1), url)

    html = await _scrape_fetch(url, referer="https://sportsurge.ws/mma/livestreams3")
    if not html:
        return []

    streams: list[str] = []
    seen: set[str] = set()

    def add(u: str) -> None:
        u = u.strip().rstrip("\"'")
        if u and u not in seen and valid_stream_url(u):
            seen.add(u)
            streams.append(u)

    if "icelz.to" in host:
        candidates = _extract_icelz_option_streams(url, html)
        if candidates:
            results = await asyncio.gather(*(_validate_hls_candidate(candidate) for candidate in candidates), return_exceptions=True)
            for candidate, ok in zip(candidates, results, strict=False):
                if ok is True:
                    add(candidate)

    # 1. Direct hereisman playlist URLs
    for m in _PLAYLIST_HOST_RE.finditer(html):
        add(m.group(0))

    # 2. All gooz embed IDs (iframe src + changeStream() onclick)
    gooz_ids = list(dict.fromkeys(_GOOZ_EMBED_RE.findall(html)))
    tasks = [_scrape_gooz_embed(gid, url) for gid in gooz_ids]
    for results in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(results, list):
            for u in results:
                add(u)

    # 3. Bare m3u8 links fallback
    for m in _M3U8_RE.finditer(html):
        add(m.group(0))

    return streams


async def _discover_ufc_event_url() -> str | None:
    """Scrape MMA listings page and return the current UFC event URL."""
    html = await _scrape_fetch(_MMA_LISTINGS_URL)
    best: str | None = None
    for m in _SPORTSURGE_EVENT_RE.finditer(html):
        event_url = m.group(1)
        start = max(0, m.start() - 300)
        end = min(len(html), m.end() + 300)
        if _UFC_CONTEXT_RE.search(html[start:end]):
            best = event_url
            break
    return best


async def _run_auto_scrape() -> list[str]:
    """Find and scrape current UFC event pages plus any configured extra scrape pages."""
    global _AUTO_SOURCES, _AUTO_SOURCES_AT
    try:
        cfg = load_config()
        scrape_pages: list[str] = []
        event_url = await _discover_ufc_event_url()
        if valid_stream_url(event_url):
            scrape_pages.append(event_url)
        scrape_pages.extend(cfg.get("stream", {}).get("scrape_urls", []))
        scrape_pages = normalize_scrape_urls(scrape_pages)
        if not scrape_pages:
            return _AUTO_SOURCES
        all_sources: list[str] = []
        seen_sources: set[str] = set()
        for page_url in scrape_pages:
            sources = await asyncio.wait_for(_scrape_page(page_url), timeout=40)
            for source in sources:
                if source in seen_sources:
                    continue
                seen_sources.add(source)
                all_sources.append(source)
        return all_sources
    except Exception as exc:
        logger.warning("auto scrape failed: %s", exc)
        return _AUTO_SOURCES


async def _auto_scrape_loop() -> None:
    global _AUTO_SOURCES, _AUTO_SOURCES_AT, _AUTO_SOURCES_LOCK
    while True:
        try:
            sources = await _run_auto_scrape()
            async with _AUTO_SOURCES_LOCK:
                if sources:
                    _AUTO_SOURCES = sources
                    _AUTO_SOURCES_AT = time.time()
                    logger.info("auto scrape: %d public source(s) refreshed", len(sources))
        except Exception as exc:
            logger.warning("auto scrape loop error: %s", exc)
        await asyncio.sleep(_AUTO_SCRAPE_INTERVAL)


_GOOZ_ORIGIN = "https://gooz.aapmains.net"
_GOOZ_REFERER = "https://gooz.aapmains.net/"


def _proxy_url(raw_url: str) -> str:
    from urllib.parse import quote
    return f"/api/proxy-hls?url={quote(raw_url, safe='')}"


async def _proxy_fetch(raw_url: str) -> tuple[bytes, str]:
    """Fetch a URL via the upstream with shared cache and coalescing."""
    now = time.monotonic()

    cached = await _PROXY_CACHE.get(raw_url, now)
    if cached:
        return cached

    # Coalesce concurrent requests for the same URL. The lock is removed as soon
    # as the fetch completes so the inflight map cannot grow without bound.
    lock = _PROXY_CACHE.lock(raw_url)
    try:
        async with lock:
            # Re-check after acquiring lock
            cached = await _PROXY_CACHE.get(raw_url, now)
            if cached:
                return cached

            try:
                body, ct = await _proxy_upstream_fetch(raw_url)
            except Exception:
                _PROXY_CACHE.record_upstream_error()
                stale = await _PROXY_CACHE.get_stale(raw_url, time.monotonic())
                if stale:
                    return stale
                raise

            ttl = _PROXY_CACHE.ttl_for(body, ct, raw_url)
            await _PROXY_CACHE.set(raw_url, body, ct, ttl, time.monotonic())
            return body, ct
    finally:
        # Drop the lock from the map after every code path, including cache hits
        # after acquiring it, so signed segment URLs cannot accumulate forever.
        await _PROXY_CACHE.release_lock(raw_url, lock)


async def _proxy_upstream_fetch(raw_url: str) -> tuple[bytes, str]:
    """Fetch through the pooled client; fall back to curl for hostile origins."""
    global _HTTPX_CLIENT
    headers = proxy_request_headers(raw_url)
    client = _HTTPX_CLIENT
    close_client = False
    if client is None:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=3.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=512, max_keepalive_connections=128),
        )
        close_client = True
    try:
        _PROXY_CACHE.record_upstream_fetch()
        response = await client.get(raw_url, headers=headers)
        response.raise_for_status()
        return response.content, response.headers.get("content-type", "")
    except (httpx.HTTPError, httpx.DecodingError) as exc:
        logger.debug("httpx proxy fetch failed for %s, trying curl fallback: %s", raw_url, exc)
        return await _proxy_upstream_fetch_curl(raw_url, headers)
    finally:
        if close_client:
            await client.aclose()


async def _proxy_upstream_fetch_curl(raw_url: str, headers: dict[str, str]) -> tuple[bytes, str]:
    curl_args = [
        "curl",
        "-sS",
        "--compressed",
        "--max-time",
        "12",
        "--connect-timeout",
        "4",
        "-L",
        "-D",
        "-",
    ]
    for name, value in headers.items():
        curl_args += ["-H", f"{name}: {value}"]
    curl_args.append(raw_url)
    proc = await asyncio.create_subprocess_exec(
        *curl_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        raw_out, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    except TimeoutError:
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.communicate()
        raise
    if proc.returncode:
        detail = (stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"curl exited {proc.returncode}: {detail}")

    return _split_curl_headers_body(raw_out)


def _split_curl_headers_body(raw_out: bytes) -> tuple[bytes, str]:
    header_end = raw_out.rfind(b"\r\n\r\n")
    separator_len = 4
    if header_end == -1:
        header_end = raw_out.rfind(b"\n\n")
        separator_len = 2
    body = raw_out[header_end + separator_len:] if header_end != -1 else raw_out
    header_block = raw_out[:header_end] if header_end != -1 else b""
    last_header_block = header_block.split(b"\r\n\r\n")[-1].split(b"\n\n")[-1]
    ct = ""
    for line in last_header_block.splitlines():
        if line.lower().startswith(b"content-type:"):
            ct = line.split(b":", 1)[1].strip().decode("utf-8", errors="replace")
            break
    return body, ct



_M3U8_URI_RE = re.compile(r'URI="([^"]+)"')


def _rewrite_m3u8(text: str, raw_url: str) -> str:
    """Rewrite all URLs inside an HLS playlist so they route through proxy_hls."""
    from urllib.parse import urljoin

    def proxify(value: str) -> str:
        value = value.strip()
        if not value or value.startswith("#"):
            return value
        # Already a local proxy URL — leave it alone
        if value.startswith("/api/proxy-hls"):
            return value
        # Resolve relative segment/key URLs against the playlist URL
        absolute = urljoin(raw_url, value)
        return _proxy_url(absolute)

    out_lines: list[str] = []
    for line in text.splitlines():
        line = line.rstrip("\r")
        # Rewrite URI="..." attributes found in #EXT-X-KEY, #EXT-X-MAP, etc.
        if line.startswith("#") and "URI=" in line:
            def _sub_uri(m: re.Match) -> str:
                return f'URI="{proxify(m.group(1))}"'
            line = _M3U8_URI_RE.sub(_sub_uri, line)
            out_lines.append(line)
            continue
        # Non-comment lines are segment/key paths or URLs
        if line and not line.startswith("#"):
            line = proxify(line)
        out_lines.append(line)
    return "\n".join(out_lines) + "\n"


async def proxy_hls(request):
    """
    Shared HLS reverse proxy — one upstream fetch per playlist/segment
    is shared across all concurrent viewers. Adds gooz.aapmains.net origin
    headers and rewrites m3u8 URLs to loop back through this proxy.
    No auth required; intended for client-side Source Changer playback.
    """
    from urllib.parse import unquote
    raw_url = unquote(request.query_params.get("url", "").strip())
    if not valid_stream_url(raw_url):
        return Response("bad url", status_code=400)

    cors_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }

    if request.method == "OPTIONS":
        return Response("", headers=cors_headers)

    try:
        body, content_type = await _proxy_fetch(raw_url)
    except Exception as exc:
        logger.warning("proxy_hls fetch failed %s: %s", raw_url, exc)
        return Response("upstream error", status_code=502)

    is_playlist = (
        "mpegurl" in content_type.lower()
        or "m3u" in content_type.lower()
        or raw_url.split("?")[0].endswith(".m3u8")
        or body.lstrip()[:7] == b"#EXTM3U"
    )

    if is_playlist:
        rewritten = _rewrite_m3u8(body.decode("utf-8", errors="replace"), raw_url)
        return Response(
            rewritten,
            media_type="application/vnd.apple.mpegurl",
            headers={**cors_headers, "Cache-Control": "max-age=1, stale-while-revalidate=4"},
        )

    return Response(
        content=body,
        media_type=content_type or "video/MP2T",
        headers={**cors_headers, "Cache-Control": "public, max-age=90, stale-while-revalidate=300"},
    )


async def source_hls(request):
    cors_headers = public_cors_headers()
    if request.method == "OPTIONS":
        return Response("", headers=cors_headers)
    source_id = str(request.path_params.get("source_id") or "").strip()
    if source_id != "server-1":
        return JSONResponse(
            {"ok": False, "error": "private cockpit sources are not public playback endpoints"},
            status_code=404,
            headers=cors_headers,
        )
    class _ServerOneRequest:
        path_params = {"path": "ufc.m3u8"}

    return await hls_proxy(_ServerOneRequest())


async def public_configured_sources(request):
    cors = public_cors_headers()
    if request.method == "OPTIONS":
        return Response("", headers=cors)
    config = load_config()
    proc = process_metrics()
    return JSONResponse({"ok": True, "sources": public_managed_sources(config, proc), "viewers": viewer_counts_snapshot()}, headers=cors)


async def public_streams(request):
    cors = public_cors_headers()
    if request.method == "OPTIONS":
        return Response("", headers=cors)
    config = load_config()
    sources = [proxied_public_source(source) for source in public_stream_inventory(config) if source.get("enabled", True)]
    return JSONResponse({"ok": True, "sources": sources, "count": len(sources)}, headers=cors)


async def add_public_stream(request):
    config = load_config()
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    url = str(body.get("url", "")).strip()
    if not valid_stream_url(url):
        return JSONResponse({"ok": False, "error": "url must be http(s)"}, status_code=400)
    sources = normalize_public_sources(config.get("public_sources", []))
    if url not in {source.get("url") for source in sources}:
        sources.append(
            {
                "id": str(body.get("id") or f"public-{len(sources) + 1}").strip(),
                "label": str(body.get("label") or f"Public {len(sources) + 1}").strip(),
                "url": url,
                "enabled": bool(body.get("enabled", True)),
                "type": str(body.get("type") or "public-hls").strip() or "public-hls",
                "description": str(body.get("description") or "").strip(),
            }
        )
    config["public_sources"] = normalize_public_sources(sources)
    save_config(config)
    event("public source added", "ok", {"url": url})
    return JSONResponse({"ok": True, "sources": [proxied_public_source(source) for source in public_stream_inventory(config)]})


async def remove_public_stream(request):
    config = load_config()
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    source_id = str(body.get("id") or "").strip()
    url = str(body.get("url") or "").strip()
    sources = normalize_public_sources(config.get("public_sources", []))
    config["public_sources"] = [
        source
        for source in sources
        if not ((source_id and source.get("id") == source_id) or (url and source.get("url") == url))
    ]
    save_config(config)
    event("public source removed", "warn", {"id": source_id, "url": url})
    return JSONResponse({"ok": True, "sources": [proxied_public_source(source) for source in public_stream_inventory(config)]})


async def live_public_events(request):
    cors = public_cors_headers()
    if request.method == "OPTIONS":
        return Response("", headers=cors)

    async def stream():
        last_payload = None
        while True:
            config = load_config()
            proc = process_metrics()
            payload = {
                "ok": True,
                "server_time": now_ms(),
                "hls": hls_metrics(config),
                "sources": public_managed_sources(config, proc),
                "viewers": viewer_counts_snapshot(),
            }
            encoded = json.dumps(payload, separators=(",", ":"))
            if encoded != last_payload:
                yield f"event: status\ndata: {encoded}\n\n"
                last_payload = encoded
            await asyncio.sleep(3)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=cors)


async def scrape_streams(request):
    """Auto-find stream URLs from a SportSurge or similar page. No auth — public."""
    cors = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }
    if request.method == "OPTIONS":
        return Response("", headers=cors)
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400, headers=cors)
    url = str(body.get("url", "")).strip()
    if not valid_stream_url(url):
        return JSONResponse({"ok": False, "error": "url must be http(s)"}, status_code=400, headers=cors)
    try:
        links = await asyncio.wait_for(_scrape_page(url), timeout=25)
    except TimeoutError:
        return JSONResponse({"ok": False, "error": "scrape timed out"}, status_code=504, headers=cors)
    except Exception as exc:
        logger.exception("scrape failed for %s", url)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500, headers=cors)
    return JSONResponse({"ok": True, "links": links, "count": len(links)}, headers=cors)


async def public_source(request):
    """Return the auto-scraped public stream sources as proxy URLs. No auth, CORS *."""
    from urllib.parse import quote as _quote
    cors = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }
    if request.method == "OPTIONS":
        return Response("", headers=cors)
    async with _AUTO_SOURCES_LOCK:
        sources = list(_AUTO_SOURCES)
        refreshed_at = _AUTO_SOURCES_AT
    proxy_urls = [f"/api/proxy-hls?url={_quote(s, safe='')}" for s in sources]
    return JSONResponse({
        "ok": True,
        "sources": proxy_urls,
        "raw": sources,
        "count": len(proxy_urls),
        "refreshed_at": refreshed_at,
        "next_refresh_in": max(0, int(_AUTO_SCRAPE_INTERVAL - (time.time() - refreshed_at))),
    }, headers=cors)


async def activate_link(request):
    """Move a link to position #1 and restart the stream with it immediately."""
    config = load_config()
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    url = str(body.get("url", "")).strip()
    if not url:
        return JSONResponse({"ok": False, "error": "url required"}, status_code=400)
    if not valid_stream_url(url):
        return JSONResponse({"ok": False, "error": "url must be http(s)"}, status_code=400)
    stream = config.setdefault("stream", {})
    sources = normalize_sources(stream.get("sources", []), stream.get("links", []))
    match = next((source for source in sources if source.get("url") == url), None)
    if match is None:
        match = {
            "id": f"source-{len(sources) + 1}",
            "label": f"Source {len(sources) + 1}",
            "url": url,
            "type": source_type_for_url(None, url),
            "enabled": True,
            "headers": {},
        }
    stream["sources"] = normalize_sources([match, *[source for source in sources if source.get("url") != url]])
    sync_links_from_sources(stream)
    save_config(config)
    event("link activated", "ok", {"url": url})
    queue_arango_insert("links", {"ts": now_ms(), "action": "activate", "url": url})
    restarted = await restart_managed_with_config("link activated")
    if restarted:
        event("running stream switched to activated link", "ok")
    return JSONResponse({"ok": True, "links": config["stream"]["links"], "sources": source_statuses(config, process_metrics())})


async def activate_source(request):
    config = load_config()
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    source_id = str(body.get("id") or body.get("source_id") or "").strip()
    url = str(body.get("url") or "").strip()
    stream = config.setdefault("stream", {})
    sources = normalize_sources(stream.get("sources", []), stream.get("links", []))
    match = next((source for source in sources if (source_id and source.get("id") == source_id) or (url and source.get("url") == url)), None)
    if not match:
        return JSONResponse({"ok": False, "error": "source not found"}, status_code=404)
    match["enabled"] = True
    stream["sources"] = normalize_sources([match, *[source for source in sources if source.get("id") != match.get("id")]])
    sync_links_from_sources(stream)
    save_config(config)
    event("source switched", "ok", {"id": match.get("id"), "label": match.get("label")})
    queue_arango_insert("links", {"ts": now_ms(), "action": "activate_source", "id": match.get("id"), "url": match.get("url")})
    restarted = await restart_managed_with_config("source switched")
    if restarted:
        event("running stream switched to selected source", "ok")
    return JSONResponse({"ok": True, "links": config["stream"]["links"], "sources": source_statuses(config, process_metrics())})


async def recover_soursignal_source(request):
    config = load_config()
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    source_id = str(body.get("id") or body.get("source_id") or "").strip()
    url = str(body.get("url") or "").strip()
    stream = config.setdefault("stream", {})
    sources = normalize_sources(stream.get("sources", []), stream.get("links", []))
    source = next((item for item in sources if (source_id and item.get("id") == source_id) or (url and item.get("url") == url)), None)
    if not source:
        return JSONResponse({"ok": False, "error": "source not found"}, status_code=404)
    if source.get("type") != "soursignal" and "soursignal.com" not in urlparse(source.get("url") or "").netloc.lower():
        return JSONResponse({"ok": False, "error": "source is not a sour signal entry"}, status_code=400)
    candidates = await _scrape_page(source.get("url"))
    if not candidates:
        candidates = await _run_auto_scrape()
    replacement = next((candidate for candidate in candidates if valid_stream_url(candidate)), None)
    if not replacement:
        return JSONResponse({"ok": False, "error": "no replacement HLS link found"}, status_code=404)
    source["url"] = replacement
    source["type"] = source_type_for_url("hls", replacement)
    stream["sources"] = normalize_sources([source, *[item for item in sources if item.get("id") != source.get("id")]])
    sync_links_from_sources(stream)
    save_config(config)
    event("sour signal source recovered", "ok", {"id": source.get("id"), "url": replacement})
    restarted = await restart_managed_with_config("sour signal source recovered")
    if restarted:
        event("running stream picked up recovered source", "ok")
    return JSONResponse({"ok": True, "source": public_source_status(source, proc=process_metrics()), "links": config["stream"]["links"], "sources": source_statuses(config, process_metrics())})


async def probe_configured_source(source):
    state = "unknown"
    message = ""
    url = source.get("url")
    try:
        if not valid_stream_url(url):
            state, message = "red", "Invalid URL"
        elif source.get("type") in {"soursignal", "page"}:
            text = await _scrape_fetch(url)
            state = "green" if text else "yellow"
            message = "Source page reachable" if text else "Source page did not respond"
        else:
            body, content_type = await _proxy_fetch(url)
            is_playlist = (
                "mpegurl" in content_type.lower()
                or "m3u" in content_type.lower()
                or str(url).split("?")[0].endswith(".m3u8")
                or body.lstrip()[:7] == b"#EXTM3U"
            )
            state = "green" if body and is_playlist else "yellow"
            message = "Playlist reachable" if state == "green" else "Source responded but was not an HLS playlist"
    except Exception as exc:
        state, message = "red", str(exc)
    SOURCE_HEALTH[source.get("id") or source.get("url")] = {
        "state": state,
        "message": message,
        "checked_at": now_ms(),
    }


async def source_health_loop():
    while True:
        try:
            config = load_config()
            for source in config.get("stream", {}).get("sources", []):
                if source.get("enabled", True):
                    await probe_configured_source(source)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("source health loop error: %s", exc)
        await asyncio.sleep(SOURCE_HEALTH_INTERVAL)


async def read_process_output(proc):
    assert proc.stdout is not None
    while True:
        try:
            line = await asyncio.wait_for(asyncio.to_thread(proc.stdout.readline), timeout=5.0)
        except TimeoutError:
            # If the child is still alive, keep polling; if it exited, readline
            # would have returned empty once the pipe closed.
            if proc.poll() is not None:
                break
            continue
        if not line:
            break
        line = line.rstrip()
        if line:
            level = classify_stream_log(line)
            item = {"ts": now_ms(), "level": level, "line": line}
            LOGS.append(item)
            if level == "error":
                ERRORS.append(item)
            if level in ("error", "warn", "info"):
                event(line, "bad" if level == "error" else level)


def build_command(config, links=None):
    stream = config.get("stream", {})
    links = normalize_links(links if links is not None else effective_stream_links(config))
    cmd = [stream.get("command", "/usr/bin/obbystreams"), "--no-color"]
    source_manifest_path = write_source_manifest(config)
    if source_manifest_path:
        cmd += ["--source-config", source_manifest_path]
    encoder = stream.get("encoder", "auto")
    if encoder:
        cmd += ["--encoder", encoder]
    if stream.get("output_dir"):
        cmd += ["--output-dir", stream["output_dir"]]
    if stream.get("ffmpeg_log_dir"):
        cmd += ["--ffmpeg-log-dir", stream["ffmpeg_log_dir"]]
    if stream.get("bitrate"):
        cmd += ["--bitrate", str(stream["bitrate"])]
    if stream.get("bitrate_720"):
        cmd += ["--bitrate-720", str(stream["bitrate_720"])]
    if stream.get("maxrate_720"):
        cmd += ["--maxrate-720", str(stream["maxrate_720"])]
    if stream.get("bufsize_720"):
        cmd += ["--bufsize-720", str(stream["bufsize_720"])]
    if stream.get("maxrate_1080"):
        cmd += ["--maxrate-1080", str(stream["maxrate_1080"])]
    if stream.get("bufsize_1080"):
        cmd += ["--bufsize-1080", str(stream["bufsize_1080"])]
    if stream.get("audio_bitrate"):
        cmd += ["--audio-bitrate", str(stream["audio_bitrate"])]
    option_flags = {
        "restart_delay": "--restart-delay",
        "max_restart_delay": "--max-restart-delay",
        "backoff_multiplier": "--backoff-multiplier",
        "backoff_jitter": "--backoff-jitter",
        "rate_limit_delay": "--rate-limit-delay",
        "quick_fail": "--quick-fail",
        "stop_after_failed_rounds": "--stop-after-failed-rounds",
        "min_assessment_seconds": "--min-assessment-seconds",
        "success_score_threshold": "--success-score-threshold",
        "failure_score_threshold": "--failure-score-threshold",
        "confirmed_failure_samples": "--confirmed-failure-samples",
        "failure_ramp_seconds": "--failure-ramp-seconds",
    }
    for key, flag in option_flags.items():
        if stream.get(key) is not None:
            cmd += [flag, str(stream[key])]
    if links:
        cmd += ["--links", *links]
    return cmd


def terminate_process_tree(proc, timeout=5):
    if not proc or proc.poll() is not None:
        return False
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=timeout)
    return True


async def stop_managed_process(reason, kill_orphans=True):
    global PROCESS, READER_TASK, STARTED_AT
    proc = PROCESS
    if not proc or proc.poll() is not None:
        if proc and proc.poll() is not None:
            RUNTIME["last_exit_code"] = proc.poll()
        PROCESS = None
        STARTED_AT = None
        STREAM_HEALTH_SCORER.reset()
        killed = await asyncio.to_thread(kill_existing_streams) if kill_orphans else []
        if killed:
            event("killed leftover stream instance(s)", "warn", {"processes": killed})
        return bool(killed)
    stopped = await asyncio.to_thread(terminate_process_tree, proc)
    if READER_TASK and not READER_TASK.done():
        READER_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await READER_TASK
    RUNTIME["last_exit_code"] = proc.poll()
    PROCESS = None
    STARTED_AT = None
    STREAM_HEALTH_SCORER.reset()
    killed = await asyncio.to_thread(kill_existing_streams) if kill_orphans else []
    if killed:
        event("killed leftover stream instance(s)", "warn", {"processes": killed})
    event(reason, "warn")
    return bool(stopped or killed)


def start_managed_process(config, links, kill_existing=True):
    global PROCESS, STARTED_AT, READER_TASK, STREAM_DESIRED_STATE
    links = normalize_links(links if links is not None else effective_stream_links(config))
    if not links:
        raise ValueError("no links configured")
    if kill_existing:
        killed = kill_existing_streams()
        if killed:
            event("killed existing stream instance(s)", "warn", {"processes": killed})
    cmd = build_command(config, links)
    try:
        PROCESS = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
    except OSError as exc:
        ERRORS.append({"ts": now_ms(), "level": "error", "line": str(exc)})
        RUNTIME["start_failures"] += 1
        raise
    STARTED_AT = now_ms()
    STREAM_DESIRED_STATE = "running"
    STREAM_HEALTH_SCORER.reset(pid=PROCESS.pid, started_at=STARTED_AT)
    READER_TASK = asyncio.create_task(read_process_output(PROCESS))
    RUNTIME["stream_starts"] += 1
    event("stream started", "ok", {"cmd": cmd, "pid": PROCESS.pid})
    return PROCESS.pid, cmd


async def restart_managed_with_config(reason):
    global PROCESS
    async with PROCESS_LOCK:
        if not PROCESS or PROCESS.poll() is not None:
            return False
        await stop_managed_process(f"stream stopped for restart: {reason}")
        event(f"restarting stream: {reason}", "warn")
        config = load_config()
        links = effective_stream_links(config)
        try:
            start_managed_process(config, links, kill_existing=True)
            RUNTIME["stream_restarts"] += 1
        except (OSError, ValueError) as exc:
            event(f"stream restart failed: {exc}", "bad")
            ERRORS.append({"ts": now_ms(), "level": "error", "line": f"stream restart failed: {exc}"})
            return False
        return True


async def start_stream(request):
    global PROCESS, STREAM_DESIRED_STATE
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    config = load_config()
    raw_links = body.get("links")
    if raw_links is not None and not isinstance(raw_links, list):
        return JSONResponse({"ok": False, "error": "links must be an array"}, status_code=400)
    links = normalize_links(raw_links) if raw_links is not None else effective_stream_links(config)
    async with PROCESS_LOCK:
        if PROCESS and PROCESS.poll() is None:
            return JSONResponse({"ok": False, "error": "managed stream already running"}, status_code=409)
        try:
            pid, cmd = start_managed_process(config, links, kill_existing=body.get("kill_existing", True))
            STREAM_DESIRED_STATE = "running"
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except OSError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, "pid": pid, "cmd": cmd})


async def stop_stream(request):
    global STREAM_DESIRED_STATE, WATCHDOG_LAST_ACTION
    async with PROCESS_LOCK:
        STREAM_DESIRED_STATE = "stopped"
        WATCHDOG_LAST_ACTION = time.monotonic()
        stopped = await stop_managed_process("stream stopped")
        return JSONResponse({"ok": True, "stopped": stopped})


async def restart_stream(request):
    global STREAM_DESIRED_STATE
    async with PROCESS_LOCK:
        STREAM_DESIRED_STATE = "running"
        if PROCESS and PROCESS.poll() is None:
            await stop_managed_process("stream stopped")
    return await start_stream(request)


async def watchdog_loop():
    global WATCHDOG_LAST_ACTION, PROCESS, STARTED_AT
    while True:
        try:
            await asyncio.sleep(2)
            config = load_config()
            stream = config.get("stream", {})
            if not stream.get("auto_recover", True):
                continue
            restart_cooldown = float(stream.get("watchdog_restart_cooldown", 20))
            async with PROCESS_LOCK:
                if not PROCESS or PROCESS.poll() is not None:
                    if PROCESS and PROCESS.poll() is not None:
                        RUNTIME["last_exit_code"] = PROCESS.poll()
                        PROCESS = None
                        STARTED_AT = None
                        STREAM_HEALTH_SCORER.reset()
                    if should_watchdog_restart_exited_process(config, STREAM_DESIRED_STATE):
                        now = time.monotonic()
                        if now - WATCHDOG_LAST_ACTION < restart_cooldown:
                            continue
                        WATCHDOG_LAST_ACTION = now
                        RUNTIME["watchdog_restarts"] += 1
                        event("watchdog restart: managed process exited", "warn")
                        try:
                            start_managed_process(config, effective_stream_links(config), kill_existing=True)
                        except (OSError, ValueError) as exc:
                            event(f"watchdog restart failed: {exc}", "bad")
                            ERRORS.append({"ts": now_ms(), "level": "error", "line": f"watchdog restart failed: {exc}"})
                    continue
                proc = process_metrics()
                hls = hls_metrics(config)
                assessment = stream_health(config, proc, hls, force=True)
                if assessment.get("decision") != "failed":
                    continue
                reasons = assessment.get("evidence", {}).get("reasons", [])
                reason = "; ".join(reasons[:3]) if reasons else assessment.get("message", "stream score confirmed failure")
                now = time.monotonic()
                if now - WATCHDOG_LAST_ACTION < restart_cooldown:
                    continue
                WATCHDOG_LAST_ACTION = now
                RUNTIME["watchdog_restarts"] += 1
                event(f"watchdog restart: {reason}", "warn")
                await stop_managed_process(f"stream stopped for watchdog: {reason}")
                links = effective_stream_links(config)
                if not links:
                    event("watchdog skipped restart because no links are configured", "warn")
                    continue
                try:
                    start_managed_process(config, links, kill_existing=True)
                except (OSError, ValueError) as exc:
                    event(f"watchdog restart failed: {exc}", "bad")
                    ERRORS.append({"ts": now_ms(), "level": "error", "line": f"watchdog restart failed: {exc}"})
        except asyncio.CancelledError:
            break
        except Exception as exc:
            event(f"watchdog loop error: {exc}", "warn")


def hls_content_type(path):
    if path.endswith(".mpd"):
        return "application/dash+xml"
    if path.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if path.endswith(".ts"):
        return "video/mp2t"
    if path.endswith(".m4s"):
        return "video/iso.segment"
    if path.endswith(".mp4"):
        return "video/mp4"
    return "application/octet-stream"


def safe_hls_path(value):
    path = str(value or "ufc.m3u8").lstrip("/")
    if not path or ".." in Path(path).parts:
        return None
    return path


def rewrite_playlist(text):
    rewritten = []
    for line in text.splitlines():
        if line and not line.startswith("#") and not line.startswith(("http://", "https://")):
            rewritten.append(f"/hls/{line.lstrip('/')}")
        else:
            rewritten.append(line)
    return "\n".join(rewritten) + "\n"


def hls_upstream_urls(config, path):
    stream = config.get("stream", {})
    public_dash_url = stream.get("public_dash_url", "")
    public_hls_url = stream.get("public_hls_url", "")
    candidates = []
    if public_dash_url and (path.endswith(".mpd") or path.endswith((".m4s", ".mp4"))):
        remote_base = public_dash_url.rsplit("/", 1)[0]
        candidates.append(public_dash_url if path.endswith(".mpd") else f"{remote_base}/{path}")
    if public_hls_url:
        remote_base = public_hls_url.rsplit("/", 1)[0]
        candidates.append(public_hls_url if path.endswith(".m3u8") else f"{remote_base}/{path}")
    fight_url = f"https://fight.nswfiles.com/stream/{path}"
    if fight_url not in candidates:
        candidates.append(fight_url)
    return candidates


async def hls_proxy(request):
    path = safe_hls_path(request.path_params.get("path", "ufc.m3u8"))
    if not path:
        return JSONResponse({"ok": False, "error": "bad hls path"}, status_code=400)

    config = load_config()
    stream = config.get("stream", {})
    output_dir = Path(stream.get("output_dir", "/var/www/live.obnoxious.lol/stream")).resolve()
    local_path = (output_dir / path).resolve()
    if (output_dir in local_path.parents or local_path == output_dir) and local_path.exists():
        if local_path.suffix == ".m3u8":
            try:
                text = local_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
            _, _, segments, _, media_playlists = _parse_playlist_lines(text.splitlines())
            if not segments:
                for media_playlist in media_playlists:
                    _, _, media_segments, _, _ = _parse_playlist_lines(_read_playlist(output_dir / media_playlist))
                    segments.extend(media_segments)
            if not segments:
                return JSONResponse({"ok": False, "error": "HLS playlist is not ready"}, status_code=404)
            return Response(
                rewrite_playlist(text),
                media_type=hls_content_type(path),
                headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
            )
        return FileResponse(
            local_path,
            media_type=hls_content_type(path),
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    upstream_urls = hls_upstream_urls(config, path)
    if not upstream_urls:
        return JSONResponse({"ok": False, "error": "public_hls_url is not configured"}, status_code=404)
    last_response = None
    last_error = None
    client = _HTTPX_CLIENT
    client_ctx = None
    if client is None:
        client_ctx = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            follow_redirects=True,
            headers={"User-Agent": "curl/8.0"},
        )
        client = client_ctx
    try:
        async with client_ctx or contextlib.nullcontext():
            for remote_url in upstream_urls:
                try:
                    response = await client.get(remote_url)
                except httpx.HTTPError as exc:
                    last_error = str(exc)
                    continue
                last_response = response
                if response.status_code < 400:
                    break
            else:
                if last_response is None:
                    return JSONResponse({"ok": False, "error": last_error or "upstream unavailable"}, status_code=502)
                return Response(
                    last_response.content,
                    status_code=last_response.status_code,
                    media_type=last_response.headers.get("content-type"),
                )
            body = rewrite_playlist(response.text).encode() if path.endswith(".m3u8") else response.content
            return Response(
                body,
                media_type=hls_content_type(path),
                headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
            )
    except httpx.HTTPError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


async def index(request):
    return FileResponse(STATIC_DIR / "index.html")


def static_asset(name, media_type=None):
    async def handler(request):
        return FileResponse(STATIC_DIR / name, media_type=media_type)

    return handler


@asynccontextmanager
async def lifespan(app):
    global WATCHDOG_TASK, ARANGO_QUEUE, ARANGO_WORKER_TASK, _AUTO_SCRAPE_TASK, _AUTO_SOURCES_LOCK, _HTTPX_CLIENT, SOURCE_HEALTH_TASK
    _AUTO_SOURCES_LOCK = asyncio.Lock()
    ARANGO_QUEUE = asyncio.Queue(maxsize=ARANGO_QUEUE_MAX)
    _HTTPX_CLIENT = httpx.AsyncClient(
        timeout=httpx.Timeout(8.0, connect=3.0),
        follow_redirects=True,
        headers={"User-Agent": _SCRAPE_UA},
        limits=httpx.Limits(max_connections=512, max_keepalive_connections=128),
    )
    ARANGO_WORKER_TASK = asyncio.create_task(arango_worker_loop())
    event("obbystreams dashboard booted", "ok")
    WATCHDOG_TASK = asyncio.create_task(watchdog_loop())
    SOURCE_HEALTH_TASK = asyncio.create_task(source_health_loop())

    async def _proxy_cache_cleanup_loop():
        while True:
            await asyncio.sleep(60)
            try:
                await _PROXY_CACHE.cleanup()
            except Exception as exc:
                logger.warning("proxy cache cleanup error: %s", exc)

    _PROXY_CACHE_TASK = asyncio.create_task(_proxy_cache_cleanup_loop())

    # Kick off first scrape immediately in the background, then loop
    async def _scrape_then_loop():
        sources = await _run_auto_scrape()
        global _AUTO_SOURCES, _AUTO_SOURCES_AT
        if sources:
            async with _AUTO_SOURCES_LOCK:
                _AUTO_SOURCES = sources
                _AUTO_SOURCES_AT = time.time()
            logger.info("initial auto scrape: %d source(s)", len(sources))
        await _auto_scrape_loop()
    _AUTO_SCRAPE_TASK = asyncio.create_task(_scrape_then_loop())
    try:
        yield
    finally:
        if _AUTO_SCRAPE_TASK:
            _AUTO_SCRAPE_TASK.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _AUTO_SCRAPE_TASK
        if WATCHDOG_TASK:
            WATCHDOG_TASK.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await WATCHDOG_TASK
        if SOURCE_HEALTH_TASK:
            SOURCE_HEALTH_TASK.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await SOURCE_HEALTH_TASK
        if _PROXY_CACHE_TASK:
            _PROXY_CACHE_TASK.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _PROXY_CACHE_TASK
        async with PROCESS_LOCK:
            await stop_managed_process("stream stopped during shutdown")
        if ARANGO_WORKER_TASK:
            ARANGO_WORKER_TASK.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ARANGO_WORKER_TASK
        if _HTTPX_CLIENT:
            with contextlib.suppress(Exception):
                await _HTTPX_CLIENT.aclose()


routes = [
    Route("/", index),
    Route("/robots.txt", static_asset("robots.txt", "text/plain")),
    Route("/sitemap.xml", static_asset("sitemap.xml", "application/xml")),
    Route("/site.webmanifest", static_asset("site.webmanifest", "application/manifest+json")),
    Route("/favicon.svg", static_asset("favicon.svg", "image/svg+xml")),
    Route("/favicon.ico", static_asset("favicon.svg", "image/svg+xml")),
    Route("/og-image.png", static_asset("og-image.png", "image/png")),
    Route("/api/health", health),
    Route("/api/auth/login", login, methods=["POST"]),
    Route("/api/status", guarded(status)),
    Route("/api/sources", guarded(list_sources), methods=["GET"]),
    Route("/api/sources/activate", guarded(activate_source), methods=["POST"]),
    Route("/api/sources/recover-soursignal", guarded(recover_soursignal_source), methods=["POST"]),
    Route("/api/config", guarded(get_config), methods=["GET"]),
    Route("/api/config", guarded(put_config), methods=["PUT"]),
    Route("/api/links", guarded(add_link), methods=["POST"]),
    Route("/api/links/remove", guarded(remove_link), methods=["POST"]),
    Route("/api/links/activate", guarded(activate_link), methods=["POST"]),
    Route("/api/scrape", scrape_streams, methods=["POST", "OPTIONS"]),
    Route("/api/public-source", public_source, methods=["GET", "OPTIONS"]),
    Route("/api/public-streams", public_streams, methods=["GET", "OPTIONS"]),
    Route("/api/public-streams", guarded(add_public_stream), methods=["POST"]),
    Route("/api/public-streams/remove", guarded(remove_public_stream), methods=["POST"]),
    Route("/api/public-configured-sources", public_configured_sources, methods=["GET", "OPTIONS"]),
    Route("/api/live", live_public_events, methods=["GET", "OPTIONS"]),
    Route("/api/viewers", viewer_counts, methods=["GET", "POST", "OPTIONS"]),
    Route("/api/proxy-hls", proxy_hls, methods=["GET", "HEAD", "OPTIONS"]),
    Route("/api/source-hls/{source_id}", source_hls, methods=["GET", "HEAD", "OPTIONS"]),
    Route("/api/stream/start", guarded(start_stream), methods=["POST"]),
    Route("/api/stream/stop", guarded(stop_stream), methods=["POST"]),
    Route("/api/stream/restart", guarded(restart_stream), methods=["POST"]),
    Route("/api/arango", guarded(arango_status)),
    Route("/api/nvidia-smi", guarded(nvidia_smi_status)),
    Route("/hls/{path:path}", hls_proxy),
    Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
]

app = Starlette(debug=False, routes=routes, lifespan=lifespan)
