#!/usr/bin/env python3
"""Obbystreams cockpit — the operator control plane for the live UFC stream.

This single-module Starlette ASGI app powers ``https://s.obby.ca``. It:

* Manages the live ffmpeg transcode (start/stop/restart, a health-scoring
  watchdog, and DASH/HLS output) that feeds the public ObbyWatcher viewer at
  ``fight.nswfiles.com`` / ``live.obnoxious.lol``.
* Runs two background scrapers: the **private-IPTV** loop (authenticated provider
  playlist → scored/probed UFC sources merged into ``stream.sources``) and the
  **public** auto-scraper (sportsurge-style pages → the red backup tiles).
* Serves the cockpit SPA plus a JSON/SSE API and a shared HLS reverse proxy that
  the viewer app calls cross-origin.

Two operator affordances layer on top of that core:

* **Persistent Stop** — ``stream.operator_stopped`` is a persisted master kill
  switch. While set, the managed ffmpeg AND both scrapers stay idle until an
  explicit Start/Restart, surviving supervisor ticks and full restarts. See
  :func:`operator_stopped` / :func:`set_operator_stopped`.
* **Source blacklist** — ``source_blacklist`` persistently blocks a stream by
  URL/id/channel/label so it can never be re-selected by a scraper or shown to
  viewers. See :func:`is_blacklisted` and the funnel filters it guards.
* **UFC auto-schedule** — the ``schedule`` section turns Stop into a *standby*:
  the ``obbyschedule`` package arms the encode ~15min before a card (ESPN
  scoreboard), stands it down once every bout is decided, and posts countdown /
  go-live / wrap-up embeds to Discord. It reaches back in only through
  :func:`schedule_start_stream` / :func:`schedule_stop_stream`, so this module
  stays the only place that knows about ``PROCESS_LOCK`` and the Stop switch.
  See ``docs/auto-schedule.md``.

Config lives at ``/etc/obbystreams/obbystreams.yaml`` and hot-reloads on mtime
change (``load_config`` has a 1s cache). Note: this module itself does NOT
hot-reload — deploying code changes requires restarting ``obbystreams.service``,
which interrupts the live encode. Tooling: ruff + ty (Astral) + pytest via uv.
"""
import asyncio
import base64
import contextlib
import csv
import glob
import hashlib
import hmac
import html
import io
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import psutil
import yaml
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from obbyschedule import EventContext, ScheduleSettings, StartResult, StartStatus, StopReason, UfcScheduler

logger = logging.getLogger("obbystreams")

# uvicorn only configures its own loggers, and the root logger it leaves at
# WARNING with no handler this module inherits — so every logger.info() here was
# being dropped on the floor. Attach our own handler once, and stop propagating
# so uvicorn's root config cannot double-print it. Self-contained on purpose: no
# systemd unit change, survives a deploy. stdout goes to journald under systemd.
if not logger.handlers:
    _log_handler = logging.StreamHandler(sys.stdout)
    _log_handler.setFormatter(logging.Formatter("%(levelname)s: %(name)s: %(message)s"))
    logger.addHandler(_log_handler)
    logger.setLevel(os.environ.get("OBBYSTREAMS_LOG_LEVEL", "INFO").upper())
    logger.propagate = False

# Operator messages routinely embed provider URLs whose path/query are bearer
# credentials. _redact_url() takes a bare URL, so text has to be matched first.
_URL_IN_TEXT_RE = re.compile(r"https?://\S+")


def _redact_message(text: str) -> str:
    """Redact any provider URLs embedded in an operator message before logging."""
    # _redact_url is defined further down; module-level names resolve at call
    # time and nothing calls this during import, so the ordering is fine.
    return _URL_IN_TEXT_RE.sub(lambda m: _redact_url(m.group(0)), text)


class _RedactProxyTargets(logging.Filter):
    """Keep provider tokens out of the access log.

    Every /api/proxy-hls request carries the upstream URL in its query string,
    and those URLs are signed provider links - bearer credentials with a few
    hours of life. uvicorn's access logger writes the full request line, so the
    journal had hundreds of them in cleartext, readable by anyone in the
    systemd-journal group and to anyone who is ever sent a log excerpt.

    Rewrites the query to `url=<redacted>` in place; the path, status and client
    are untouched, so the log stays just as useful for debugging.
    """

    _PATTERN = re.compile(r"(\burl=)[^\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(
                self._PATTERN.sub(r"\1<redacted>", a) if isinstance(a, str) else a
                for a in record.args
            )
        if isinstance(record.msg, str):
            record.msg = self._PATTERN.sub(r"\1<redacted>", record.msg)
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactProxyTargets())
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
# Exact ingest pool handed to the currently managed ffmpeg process. Config can
# change while ffmpeg is still running, so source verification must compare
# against what was actually launched rather than merely what is now on disk.
MANAGED_LINKS: tuple[str, ...] = ()
READER_TASK = None
PROCESS_LOCK = asyncio.Lock()
PRIVATE_PROBE_LOCK = asyncio.Lock()
PRIVATE_REFRESH_LOCK = asyncio.Lock()
WATCHDOG_TASK = None
WATCHDOG_LAST_ACTION = 0.0
STREAM_DESIRED_STATE = "running"
# UFC auto-schedule (obbyschedule.UfcScheduler); constructed in lifespan.
SCHEDULER: UfcScheduler | None = None
SCHEDULE_TASK = None
# The card the scheduler is currently tracking, as handed to the source scraper.
# None outside an event window, which restores the older date-window-only
# matching for anyone running the cockpit without the auto-schedule.
ACTIVE_EVENT_CONTEXT: EventContext | None = None
# Mid-card source switching bookkeeping, reset per tracked card: how many swaps
# this card has cost viewers, when the last one landed, and how many consecutive
# cycles have judged the live feed wrong (a single bad sample is not enough).
SOURCE_SWITCH_STATE: dict[str, Any] = {
    "event_id": None,
    "switches": 0,
    "last_switch_at": 0.0,
    "mismatch_samples": 0,
    "acquire_attempts": 0,
    "last_reasons": [],
    "last_error": "",
    "selected_confidence": None,
}
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
PRIVATE_IPTV_TASK = None

# Strong references to fire-and-forget background tasks, so the event loop does
# not garbage-collect them mid-flight (see RUF006).
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _spawn_background(coro):
    """Schedule a fire-and-forget coroutine while retaining a strong reference."""
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


SOURCE_HEALTH_TASK = None
SOURCE_HEALTH_INTERVAL = 15
SOURCE_HEALTH: dict[str, dict] = {}
VIEWER_SESSION_TTL = 45
VIEWER_SESSIONS: dict[str, dict[str, Any]] = {}
VIEWER_LOCK = asyncio.Lock()


# Monotonic timestamps of every managed-encode start, for the instability alert.
# One restart is routine (each card-segment transition causes one by design); a
# burst of them is the thing nobody was told about — 2026-08-08 saw 25 in a day
# with no notification anywhere.
STREAM_START_TIMES: deque[float] = deque(maxlen=64)
STREAM_INSTABILITY_WINDOW_SECONDS = 900.0
STREAM_INSTABILITY_THRESHOLD = 4
STREAM_INSTABILITY_COOLDOWN_SECONDS = 1800.0
_LAST_INSTABILITY_ALERT_AT: float | None = None

RUNTIME: dict[str, Any] = {
    "stream_starts": 0,
    "stream_restarts": 0,
    "watchdog_restarts": 0,
    "start_failures": 0,
    "last_exit_code": None,
    "arango_dropped_writes": 0,
    "arango_write_failures": 0,
}


# ---------------------------------------------------------------------------
# Proxy cache — bounded TTL cache for shared HLS proxy responses.
# ---------------------------------------------------------------------------
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
        """Initialize the cache with size cap and per-kind TTLs, plus a serializing lock and stats counters."""
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
        """Return the fresh (body, content_type) for raw_url, or None on miss; eagerly drops a fully-expired entry."""
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
        """Return a within-stale-window entry (past TTL but not yet evicted) for serving during upstream failures."""
        cached = self._cache.get(raw_url)
        if cached and cached["stale_until"] > now:
            self._stats["stale_hits"] += 1
            return cached["body"], cached["content_type"]
        return None

    def lock(self, raw_url: str) -> asyncio.Lock:
        """Return (creating if needed) the per-URL inflight lock used to coalesce concurrent upstream fetches."""
        return self._inflight.setdefault(raw_url, asyncio.Lock())

    async def set(self, raw_url: str, body: bytes, ct: str, ttl: float, now: float, stale_ttl: float | None = None) -> None:
        """Store a response under the internal lock, updating byte stats and evicting the oldest half if over max_size."""
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
        """Drop the inflight lock for raw_url once unused, so signed segment URLs cannot accumulate forever."""
        async with self._lock:
            if self._inflight.get(raw_url) is lock and not lock.locked():
                self._inflight.pop(raw_url, None)

    def record_upstream_fetch(self) -> None:
        """Increment the upstream-fetch counter (one real origin request)."""
        self._stats["upstream_fetches"] += 1

    def record_upstream_error(self) -> None:
        """Increment the upstream-error counter."""
        self._stats["upstream_errors"] += 1

    def stats(self) -> dict:
        """Return a snapshot of counters plus current entry/inflight sizes and configured TTLs."""
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
        """Acquire the lock and prune all expired entries and unused inflight locks."""
        async with self._lock:
            await self._cleanup_unlocked(time.monotonic())

    async def _cleanup_unlocked(self, now: float) -> None:
        """Prune expired cache entries and unused inflight locks; caller must already hold self._lock."""
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
        """Pick the playlist TTL (short) vs segment TTL (long) by sniffing content-type, extension, and #EXTM3U magic."""
        is_playlist = (
            "mpegurl" in ct.lower()
            or "m3u" in ct.lower()
            or raw_url.split("?")[0].endswith(".m3u8")
            or body.lstrip()[:7] == b"#EXTM3U"
        )
        return self._playlist_ttl if is_playlist else self._segment_ttl


_PROXY_CACHE = _ProxyCache()


def now_ms():
    """Current wall-clock time in integer milliseconds since the epoch."""
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


DEFAULT_CONFIG: dict[str, Any] = {
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
        # Persisted operator "master kill switch". When True, a human hit Stop:
        # the managed ffmpeg stays down and BOTH scrapers pause until an explicit
        # Start/Restart clears it. Survives supervisor ticks and full restarts.
        "operator_stopped": False,
        # Why the stream is down: "manual" (a human hit Stop) or "schedule" (the
        # auto-scheduler stood it down after a card). Drives the cockpit banner —
        # with auto-schedule on, a Stop reads as STANDBY rather than STOPPED.
        "stop_reason": "",
        "locked_source_id": "",
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
    "private_iptv": {
        "enabled": False,
        "paused": False,
        "provider_url": "https://iptorrents.com/iptv",
        "playlist_url": "",
        "playlist_link_selector": "m3uDownloadBtn",
        "timezone": "Canada/Pacific",
        "refresh_interval_seconds": 900,
        "max_candidates": 12,
        "min_score": 70,
        "probe_candidates": True,
        "probe_timeout_seconds": 10,
        "disable_stream_when_inactive": True,
        "connection_limit": 2,
        "reserve_spare_when_streaming": True,
        "protect_live_stream_on_refresh": True,
        "keep_stream_live_when_inactive": True,
        "auto_start_when_active": True,
        "auto_source_prefix": "private-iptv",
        "headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://iptorrents.com/t",
        },
        "cookies": {},
        "keywords": [
            "ufc",
            "mma",
            "fight night",
            "prelims",
            "main card",
            "ppv main card",
        ],
        "required_keywords": ["ufc"],
        "reject_keywords": [
            "no event",
            "no scheduled event",
            "replay",
            "classic",
            "24/7",
            "post fight press conference",
            "pre show",
        ],
        "date_window_hours": 30,
        "require_date_window_match": True,
        # Event-aware discovery. While the auto-schedule is tracking a card, the
        # scraper is handed that card's identity and must positively match it —
        # matching only "UFC" plus a date window is what put the previous week's
        # channels on air for a whole event.
        "event_refresh_interval_seconds": 180,
        "switch_cooldown_seconds": 300,
        "switch_confirm_samples": 2,
        "max_switches_per_card": 6,
        "public_fallback_after_attempts": 4,
    },
    "public_sources": [],
    # Persistent per-source blacklist. Any scraped/auto-selected or manually added
    # source whose URL/id/channel/label matches an entry here is filtered out of
    # every scraper cycle and every viewer-facing list, so a blocked stream can
    # never reappear. Entry shape: {url, id, label, channel, reason, added_at}.
    "source_blacklist": [],
    "watcher_news": [],
    # UFC auto-schedule. While `enabled`, the operator Stop becomes a *standby*:
    # the scheduler arms the encode `lead_minutes` before a card's first segment
    # and stands it back down once every bout is decided. Backed by the public
    # ESPN scoreboard; see the obbyschedule package.
    "schedule": ScheduleSettings.from_config({}).to_config(),
    "arangodb": {
        "enabled": True,
        "url": "http://127.0.0.1:8529",
        "database": "obbystreams",
        "username": "obbystreams_app",
        "password": "",
    },
}


# ---------------------------------------------------------------------------
# Value coercion — safe numeric parsing and nvidia-smi text/field helpers.
# ---------------------------------------------------------------------------
def safe_number(value, fallback, minimum=None):
    """Coerce value to float, using fallback on failure and clamping to minimum when given."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        n = float(fallback)
    if minimum is not None:
        n = max(float(minimum), n)
    return n


def safe_int(value, fallback, minimum=None):
    """Coerce value to int via safe_number (fallback + optional minimum clamp)."""
    return int(safe_number(value, fallback, minimum=minimum))


def safe_float_or_none(value):
    """Coerce value to float, returning None instead of raising on invalid input."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def smi_text(value):
    """Normalize an nvidia-smi cell to a stripped string, mapping N/A / Not Supported / '-' placeholders to None."""
    text = str(value or "").strip()
    if text in {"", "N/A", "[N/A]", "Not Supported", "[Not Supported]", "-"}:
        return None
    return text


def smi_float(value):
    """Parse an nvidia-smi cell to float, or None if empty/placeholder/unparseable."""
    text = smi_text(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def smi_int(value):
    """Parse an nvidia-smi cell to int (via smi_float), or None."""
    number = smi_float(value)
    if number is None:
        return None
    return int(number)


def smi_percent(part, whole):
    """Return part/whole as a percentage rounded to 1dp, or None when either side is missing/zero."""
    if part is None or whole in (None, 0):
        return None
    return round((float(part) / float(whole)) * 100, 1)


# ---------------------------------------------------------------------------
# URL validation & SSRF guard — structural checks plus DNS-based non-public filtering.
# ---------------------------------------------------------------------------
def valid_stream_url(value):
    """True if value is a well-formed http(s) URL with a netloc (structural check only, no SSRF filtering)."""
    text = str(value or "").strip()
    if not text:
        return False
    parsed = urlparse(text)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


# --- SSRF guard -----------------------------------------------------------
# proxy_hls fetches attacker-controlled URLs on behalf of anonymous callers.
# Without these checks the endpoint is a blind SSRF / open proxy that can reach
# loopback, RFC1918, link-local (169.254.169.254 cloud metadata), and other
# internal services. We resolve the host and reject if ANY resolved address is
# non-public, and we re-validate every redirect hop before following it.

# Extra ranges not consistently covered by is_private across Python versions
# (e.g. RFC 6598 CGNAT is only in is_private on 3.13+). Blocked explicitly so
# the guard does not depend on the interpreter's stdlib version.
_EXTRA_BLOCKED_NETS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "100.64.0.0/10",   # RFC 6598 CGNAT / shared address space
        "192.0.0.0/24",    # IETF protocol assignments
        "198.18.0.0/15",   # RFC 2544 benchmarking
        "64:ff9b::/96",    # NAT64
    )
)


def _ip_is_blocked(ip_str):
    """True if ip_str is unparseable or falls in any private/loopback/link-local/reserved/CGNAT range (SSRF deny)."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable address: refuse
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True
    return any(ip in net for net in _EXTRA_BLOCKED_NETS)


def host_resolves_public(host):
    """True only if every A/AAAA record for host is a routable public address."""
    if not host:
        return False
    host = host.strip().strip("[]")
    # Reject bare IP literals that fall in blocked ranges without a DNS lookup.
    try:
        ipaddress.ip_address(host)
        return not _ip_is_blocked(host)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    if not infos:
        return False
    return all(not _ip_is_blocked(info[4][0]) for info in infos)


def url_is_safe_public(url):
    """Structural validation plus DNS-resolution-based SSRF filtering."""
    if not valid_stream_url(url):
        return False
    return host_resolves_public(urlparse(url).hostname)


async def url_is_safe_public_async(url):
    """Async wrapper for url_is_safe_public that runs the blocking getaddrinfo off the event loop."""
    # getaddrinfo blocks; keep it off the event loop.
    return await asyncio.to_thread(url_is_safe_public, url)


def request_origin(request):
    """Best-effort request origin: the Origin header, else scheme+host derived from Referer, else ''."""
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
    """True only when the request's origin scheme+host exactly matches the request URL (same-origin write guard)."""
    candidate = request_origin(request)
    if not candidate:
        return False
    parsed = urlparse(candidate)
    request_url = request.url
    return parsed.scheme == request_url.scheme and parsed.netloc == request_url.netloc


# ---------------------------------------------------------------------------
# URL / link / source normalization — canonicalize config lists into stable dicts.
# ---------------------------------------------------------------------------
def normalize_links(raw_links):
    """De-duplicate raw_links into an ordered list of valid http(s) URLs (invalid entries dropped)."""
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
    """Return an explicit source type when recognized, else infer 'soursignal' from the host or default to 'hls'."""
    candidate = str(raw_type or "").strip().lower()
    if candidate in {"hls", "soursignal", "page", "external"}:
        return candidate
    host = urlparse(str(url or "")).netloc.lower()
    if host.endswith("soursignal.com"):
        return "soursignal"
    return "hls"


def is_soursignal_url(url):
    """True if url's host ends with soursignal.com."""
    return urlparse(str(url or "")).netloc.lower().endswith("soursignal.com")


def is_private_soursignal_source(source, private_cfg=None):
    """True if a source is a private-IPTV soursignal feed (by type, host, or auto_source_prefix id)."""
    private_cfg = private_cfg or {}
    prefix = str(private_cfg.get("auto_source_prefix") or "private-iptv")
    source_id = str(source.get("id") or "")
    return (
        str(source.get("type") or "").lower() == "soursignal"
        or is_soursignal_url(source.get("url"))
        or source_id.startswith(prefix + "-")
    )


def normalize_source_headers(raw_headers):
    """Return a clean {name: value} header dict, dropping entries with empty or CR/LF/':'-injected names."""
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
    """Canonicalize ingest sources into de-duplicated dicts with unique slugified ids, appending fallback_links as bare sources."""
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
        # Provenance for the auto-schedule: which card this feed was discovered
        # for, and when. Without it a source list is indistinguishable from last
        # week's, which is exactly how a stale channel goes back on air.
        event_id = str(raw.get("event_id") or "").strip()
        if event_id:
            source["event_id"] = event_id
        discovered_at = safe_int(raw.get("discovered_at"), 0, minimum=0)
        if discovered_at:
            source["discovered_at"] = discovered_at
        confidence = str(raw.get("match_confidence") or "").strip().lower()
        if confidence in {"exact", "dated", "generic-ufc"}:
            source["match_confidence"] = confidence
        segment_label = str(raw.get("segment_label") or "").strip()
        segment_start = str(raw.get("segment_start") or "").strip()
        if segment_label:
            source["segment_label"] = segment_label
        if segment_start:
            source["segment_start"] = segment_start
        for score_key in ("selection_score", "probe_score"):
            score = safe_int(raw.get(score_key), 0)
            if score:
                source[score_key] = score
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
    """Canonicalize viewer-facing public sources into de-duplicated dicts (manual origin, unique ids, optional description)."""
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


def _blacklist_keys_for(url, source_id="", label="", channel=""):
    """Return every normalized key a source exposes to the blacklist matcher.

    Keys are namespaced so different kinds never collide: the full normalized
    URL and its query-stripped form (for CDN token rotation), the id as ``id:``,
    and channel/label together as ``name:`` (human names match either a source's
    title or its tvg-name). Keeping ``id:`` separate from ``name:`` prevents an
    id-block (e.g. a positional ``auto-public-3``) from matching an unrelated
    source whose label happens to equal that id.
    """
    keys: set[str] = set()
    text = str(url or "").strip()
    if valid_stream_url(text):
        parsed = urlparse(text)
        path = parsed.path.rstrip("/")
        base = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"
        keys.add(f"{base}?{parsed.query}" if parsed.query else base)
        # Query-stripped key survives CDN token rotation, but only add it when the
        # path itself is distinguishing — with an empty path it collapses to the
        # bare host and would over-match every query-keyed stream on that host.
        if path:
            keys.add(base)
    id_value = str(source_id or "").strip().lower()
    if id_value:
        keys.add(f"id:{id_value}")
    for extra in (channel, label):
        value = str(extra or "").strip().lower()
        if value:
            keys.add(f"name:{value}")
    return keys


def normalize_blacklist(raw_entries):
    """Normalize the persisted ``source_blacklist`` into a stable, de-duplicated
    list of ``{url, id, label, channel, reason, added_at}`` dicts."""
    entries = []
    seen = set()
    values = raw_entries if isinstance(raw_entries, list) else []
    for item in values:
        if isinstance(item, str):
            raw = {"url": item}
        elif isinstance(item, dict):
            raw = item
        else:
            continue
        url = str(raw.get("url") or "").strip()
        source_id = str(raw.get("id") or "").strip()
        label = str(raw.get("label") or raw.get("title") or "").strip()
        channel = str(raw.get("channel") or (raw.get("attrs") or {}).get("tvg-name") or "").strip()
        if not (url or source_id or label or channel):
            continue
        dedupe_key = (url.lower(), source_id.lower(), label.lower(), channel.lower())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        entries.append(
            {
                "url": url,
                "id": source_id,
                "label": label,
                "channel": channel,
                "reason": str(raw.get("reason") or "").strip(),
                "added_at": safe_int(raw.get("added_at"), 0),
            }
        )
    return entries


def blacklist_index(blacklist):
    """Precompute the union of all blacklist match keys for O(1) membership
    checks inside hot scraper loops."""
    index: set[str] = set()
    for entry in blacklist or []:
        index |= _blacklist_keys_for(entry.get("url"), entry.get("id"), entry.get("label"), entry.get("channel"))
    return index


def is_blacklisted(entry_or_url, blacklist):
    """True if a source matches any blacklist entry.

    ``entry_or_url`` may be a URL string, a normalized source dict
    (``{url,id,label,...}``), or a raw parsed m3u entry (``{url,title,attrs}``).
    ``blacklist`` may be the raw list or a precomputed :func:`blacklist_index`
    set (pass the set when checking many entries in a loop).
    """
    index = blacklist if isinstance(blacklist, set) else blacklist_index(blacklist)
    if not index:
        return False
    if isinstance(entry_or_url, str):
        keys = _blacklist_keys_for(entry_or_url)
    elif isinstance(entry_or_url, dict):
        attrs = entry_or_url.get("attrs") or {}
        keys = _blacklist_keys_for(
            entry_or_url.get("url"),
            entry_or_url.get("id"),
            entry_or_url.get("label") or entry_or_url.get("title"),
            entry_or_url.get("channel") or attrs.get("tvg-name"),
        )
    else:
        return False
    return bool(keys & index)


def normalize_string_list(raw_values):
    """Return a list of trimmed non-empty strings with case-insensitive de-duplication, preserving order."""
    values = raw_values if isinstance(raw_values, list) else []
    normalized = []
    seen = set()
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


def normalize_news_entries(raw_entries):
    """Canonicalize watcher_news items (unique ids, clamped title/body, validated tone, timestamps), sorted pinned-first, capped at 50."""
    values = raw_entries if isinstance(raw_entries, list) else []
    entries = []
    seen_ids = set()
    current_ms = now_ms()
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()[:140]
        body = str(raw.get("body") or raw.get("message") or "").strip()[:4000]
        if not title and not body:
            continue
        raw_id = str(raw.get("id") or "").strip()
        entry_id = re.sub(r"[^a-zA-Z0-9_.-]+", "-", raw_id).strip("-").lower()
        if not entry_id:
            entry_id = f"news-{index + 1}"
        base_id = entry_id
        suffix = 2
        while entry_id in seen_ids:
            entry_id = f"{base_id}-{suffix}"
            suffix += 1
        tone = str(raw.get("tone") or raw.get("level") or "info").strip().lower()
        if tone not in {"info", "ok", "warn", "bad", "neutral"}:
            tone = "info"
        created_at = safe_int(raw.get("created_at") or raw.get("published_at") or current_ms, current_ms, minimum=0)
        updated_at = safe_int(raw.get("updated_at") or created_at, created_at, minimum=0)
        entry = {
            "id": entry_id,
            "title": title,
            "body": body,
            "tone": tone,
            "visible": bool(raw.get("visible", True)),
            "pinned": bool(raw.get("pinned", False)),
            "created_at": created_at,
            "updated_at": max(created_at, updated_at),
        }
        link_url = str(raw.get("link_url") or raw.get("url") or "").strip()
        if valid_stream_url(link_url):
            entry["link_url"] = link_url
            entry["link_label"] = str(raw.get("link_label") or "Open").strip()[:80] or "Open"
        seen_ids.add(entry_id)
        entries.append(entry)
    entries.sort(key=lambda item: (not item.get("pinned"), -int(item.get("updated_at") or 0)))
    return entries[:50]


def normalize_private_iptv(raw_config):
    """Merge raw_config over the private_iptv defaults and coerce every field (bools, ints, headers, cookies, keyword lists)."""
    defaults = json.loads(json.dumps(DEFAULT_CONFIG["private_iptv"]))
    if isinstance(raw_config, dict):
        defaults.update(raw_config)
    defaults["enabled"] = bool(defaults.get("enabled", False))
    defaults["paused"] = bool(defaults.get("paused", False))
    defaults["provider_url"] = str(defaults.get("provider_url") or "").strip()
    defaults["playlist_url"] = str(defaults.get("playlist_url") or "").strip()
    defaults["playlist_link_selector"] = str(defaults.get("playlist_link_selector") or "m3uDownloadBtn").strip()
    defaults["timezone"] = str(defaults.get("timezone") or "Canada/Pacific").strip()
    defaults["refresh_interval_seconds"] = safe_int(defaults.get("refresh_interval_seconds"), 900, minimum=120)
    defaults["max_candidates"] = safe_int(defaults.get("max_candidates"), 12, minimum=1)
    defaults["min_score"] = safe_int(defaults.get("min_score"), 70, minimum=1)
    defaults["probe_candidates"] = bool(defaults.get("probe_candidates", True))
    defaults["probe_timeout_seconds"] = safe_number(defaults.get("probe_timeout_seconds"), 10, minimum=3)
    defaults["disable_stream_when_inactive"] = bool(defaults.get("disable_stream_when_inactive", True))
    defaults["connection_limit"] = safe_int(defaults.get("connection_limit"), 2, minimum=1)
    defaults["reserve_spare_when_streaming"] = bool(defaults.get("reserve_spare_when_streaming", True))
    defaults["protect_live_stream_on_refresh"] = bool(defaults.get("protect_live_stream_on_refresh", True))
    if "keep_stream_live_when_inactive" in defaults:
        defaults["keep_stream_live_when_inactive"] = bool(defaults.get("keep_stream_live_when_inactive", True))
    else:
        defaults["keep_stream_live_when_inactive"] = not defaults["disable_stream_when_inactive"]
    defaults["auto_start_when_active"] = bool(defaults.get("auto_start_when_active", True))
    defaults["auto_source_prefix"] = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(defaults.get("auto_source_prefix") or "private-iptv")).strip("-").lower() or "private-iptv"
    defaults["headers"] = normalize_source_headers(defaults.get("headers"))
    cookies = {}
    if isinstance(defaults.get("cookies"), dict):
        for key, value in defaults["cookies"].items():
            name = str(key or "").strip()
            if name and not any(ch in name for ch in "\r\n;="):
                cookies[name] = str(value or "").strip()
    defaults["cookies"] = cookies
    defaults["keywords"] = normalize_string_list(defaults.get("keywords")) or list(DEFAULT_CONFIG["private_iptv"]["keywords"])
    defaults["required_keywords"] = normalize_string_list(defaults.get("required_keywords")) or ["ufc"]
    defaults["reject_keywords"] = normalize_string_list(defaults.get("reject_keywords"))
    defaults["date_window_hours"] = safe_number(defaults.get("date_window_hours"), 30, minimum=1)
    defaults["require_date_window_match"] = bool(defaults.get("require_date_window_match", True))
    defaults["event_refresh_interval_seconds"] = safe_int(defaults.get("event_refresh_interval_seconds"), 180, minimum=30)
    defaults["switch_cooldown_seconds"] = safe_number(defaults.get("switch_cooldown_seconds"), 300, minimum=0)
    defaults["switch_confirm_samples"] = safe_int(defaults.get("switch_confirm_samples"), 2, minimum=1)
    defaults["max_switches_per_card"] = safe_int(defaults.get("max_switches_per_card"), 6, minimum=1)
    defaults["public_fallback_after_attempts"] = safe_int(defaults.get("public_fallback_after_attempts"), 4, minimum=1)
    return defaults


def proxied_public_source(source):
    """Strip a public source's headers (exposing only has_headers) and add a proxy playback_url for viewer use."""
    safe = {key: value for key, value in source.items() if key != "headers"}
    if source.get("headers"):
        safe["has_headers"] = True
    return {
        **safe,
        "playback_url": _proxy_url(source.get("url", "")),
    }


def auto_public_sources():
    """Build read-only public source dicts from the current auto-scraped playlist URLs."""
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
    """Viewer-facing public source list: operator-added ``public_sources`` plus
    de-duplicated auto-scraped tiles, with any blacklisted source removed."""
    bl_index = blacklist_index(config.get("source_blacklist"))
    manual = [s for s in normalize_public_sources(config.get("public_sources", [])) if not is_blacklisted(s, bl_index)]
    seen_urls = {source.get("url") for source in manual}
    auto = [
        source
        for source in auto_public_sources()
        if source.get("url") not in seen_urls and not is_blacklisted(source, bl_index)
    ]
    return [*manual, *auto]


def enabled_source_links(config):
    """Normalized URL list of all enabled ingest sources in config.stream.sources."""
    stream = config.get("stream", {})
    return normalize_links([s.get("url") for s in stream.get("sources", []) if s.get("enabled", True)])


def sync_links_from_sources(stream):
    """Rebuild stream['links'] from the enabled sources' URLs and return it (keeps the legacy links list in sync)."""
    stream["links"] = normalize_links([s.get("url") for s in stream.get("sources", []) if s.get("enabled", True)])
    return stream["links"]


def normalize_scrape_urls(raw_urls):
    """De-duplicate one-or-many raw scrape page URLs into an ordered list of valid http(s) URLs."""
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
    """Deep-merge config over DEFAULT_CONFIG and coerce every section/field to its canonical, validated form."""
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    if not isinstance(config, dict):
        return merged
    for section in ("server", "dashboard", "stream", "arangodb"):
        raw_section = config.get(section, {})
        if not isinstance(raw_section, dict):
            continue
        merged[section].update(raw_section)
    merged["private_iptv"] = normalize_private_iptv(config.get("private_iptv", merged.get("private_iptv", {})))
    if "public_sources" in config:
        merged["public_sources"] = config.get("public_sources", [])
    merged["public_sources"] = normalize_public_sources(merged.get("public_sources", []))
    if "source_blacklist" in config:
        merged["source_blacklist"] = config.get("source_blacklist", [])
    merged["source_blacklist"] = normalize_blacklist(merged.get("source_blacklist", []))
    if "watcher_news" in config:
        merged["watcher_news"] = config.get("watcher_news", [])
    merged["watcher_news"] = normalize_news_entries(merged.get("watcher_news", []))
    # `schedule` is a top-level section, and this function rebuilds the config
    # from DEFAULT_CONFIG on every save — without this line an operator's
    # schedule settings would be silently erased the first time anything else
    # calls save_config().
    merged["schedule"] = ScheduleSettings.from_config(config.get("schedule", merged.get("schedule", {}))).to_config()
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
    stream["operator_stopped"] = bool(stream.get("operator_stopped", False))
    stop_reason = str(stream.get("stop_reason") or "").strip().lower()
    stream["stop_reason"] = stop_reason if stop_reason in {member.value for member in StopReason} else ""
    stream["locked_source_id"] = str(stream.get("locked_source_id") or "").strip()
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
    """Snapshot of the auto-scraped public source URLs, normalized."""
    return normalize_links(list(_AUTO_SOURCES))


def ordered_stream_sources(config):
    """Enabled sources with the persistent operator lock first."""
    stream = config.setdefault("stream", {})
    sources = [source for source in normalize_sources(stream.get("sources", []), stream.get("links", [])) if source.get("enabled", True)]
    locked_id = str(stream.get("locked_source_id") or "")
    if locked_id:
        locked = next((source for source in sources if source.get("id") == locked_id), None)
        if locked:
            sources = [locked, *[source for source in sources if source.get("id") != locked_id]]
    return sources


def effective_stream_links(config):
    """Active ingest link pool, with a locked source pinned first."""
    stream = config.setdefault("stream", {})
    return normalize_links([source.get("url") for source in ordered_stream_sources(config)]) or normalize_links(stream.get("links", []))


# ---------------------------------------------------------------------------
# Config load / save & public serialization — disk I/O, caching, redaction.
# ---------------------------------------------------------------------------
def load_config(fresh=False):
    """Load and normalize the YAML config, served from a ~1s mtime-aware cache unless fresh=True; never raises (falls back to defaults)."""
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
    """Normalize and atomically write config to disk (tmp + os.replace), then invalidate the read cache."""
    tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
    normalized = normalize_config(config)
    with tmp.open("w", encoding="utf-8") as f:
        yaml.safe_dump(normalized, f, sort_keys=False)
    os.replace(tmp, CONFIG_PATH)
    # Replace the cache object so the next load_config() sees the new values even
    # if load_config() has already run and reassigned _CONFIG_CACHE to a fresh dict.
    global _CONFIG_CACHE
    _CONFIG_CACHE = {"config": None, "mtime": 0.0, "at": 0.0}


def redact_headers(headers):
    """Return a copy of headers with cookie/authorization/token/key/secret/pass values masked as '***'."""
    safe = {}
    for key, value in (headers or {}).items():
        lowered = str(key or "").lower()
        if any(token in lowered for token in ("cookie", "authorization", "token", "key", "secret", "pass")):
            safe[key] = "***"
        else:
            safe[key] = value
    return safe


def public_config(config):
    """Deep-copy config with all secrets stripped (passwords, tokens, playlist URL, cookies, source headers) for API/UI exposure."""
    safe = json.loads(json.dumps(config))
    safe.get("dashboard", {}).pop("password", None)
    safe.get("dashboard", {}).pop("session_token", None)
    safe.get("arangodb", {}).pop("password", None)
    if isinstance(safe.get("private_iptv"), dict):
        private = safe["private_iptv"]
        if private.get("playlist_url"):
            private["playlist_url"] = "***"
        if private.get("cookies"):
            private["cookies"] = dict.fromkeys(private.get("cookies", {}), "***")
        if private.get("headers"):
            private["headers"] = redact_headers(private.get("headers", {}))
    schedule_section = safe.get("schedule")
    if isinstance(schedule_section, dict):
        notify_section = schedule_section.get("notify")
        # The Discord webhook URL is a bearer credential — anyone holding it can
        # post to the channel — so it must never leave the box in an API payload.
        if isinstance(notify_section, dict) and notify_section.get("discord_webhook_url"):
            notify_section["discord_webhook_url"] = "***"
    for source in safe.get("stream", {}).get("sources", []) or []:
        source.pop("headers", None)
    safe["public_sources"] = [proxied_public_source(source) for source in public_stream_inventory(config)]
    return safe


def public_news_entries(config, include_hidden=False):
    """Return normalized watcher_news entries (visible-only unless include_hidden), capped at 20."""
    entries = normalize_news_entries(config.get("watcher_news", []))
    if not include_hidden:
        entries = [entry for entry in entries if entry.get("visible", True)]
    return entries[:20]


def public_cors_headers():
    """Standard permissive CORS + no-cache header set for the public viewer-facing endpoints."""
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, HEAD, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Cache-Control": "no-cache",
    }


def source_manifest(config):
    """Build the {sources:[{url,headers}]} manifest of enabled ingest sources consumed by the ffmpeg wrapper."""
    return {
        "sources": [
            {
                "url": source.get("url"),
                "headers": source.get("headers") or {},
            }
            for source in ordered_stream_sources(config)
            if source.get("enabled", True) and source.get("url")
        ]
    }


def write_source_manifest(config):
    """Write the source manifest JSON to source_manifest_path; return the path, or None on OSError (logged to ERRORS)."""
    path = Path(config.get("stream", {}).get("source_manifest_path") or DEFAULT_CONFIG["stream"]["source_manifest_path"])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(source_manifest(config), indent=2), encoding="utf-8")
        return str(path)
    except OSError as exc:
        ERRORS.append({"ts": now_ms(), "level": "error", "line": f"source manifest write failed: {exc}"})
        return None


def public_source_status(source, index=0, proc=None):
    """Assemble a UI-facing status dict for one ingest source (health, preferred/in-process flags, viewer count)."""
    source_id = source.get("id") or f"source-{index + 1}"
    health = SOURCE_HEALTH.get(source_id, {})
    active_managed = bool(proc and proc.get("managed") and index == 0)
    health_state = health.get("state")
    if active_managed and health_state in {None, "", "unknown"}:
        health_state = "green"
    return {
        "id": source_id,
        "label": source.get("label") or f"Source {index + 1}",
        "type": source.get("type") or source_type_for_url(None, source.get("url")),
        "url": source.get("url"),
        "enabled": bool(source.get("enabled", True)),
        "preferred": index == 0,
        "locked": bool(source.get("locked")),
        "in_process": active_managed,
        "health": health_state or "unknown",
        "health_message": health.get("message") or "",
        "checked_at": health.get("checked_at"),
        "viewer_count": viewer_counts_snapshot().get("by_source", {}).get(source_id, 0),
    }


def source_statuses(config, proc=None):
    """Managed ingest sources with live health, blacklisted entries filtered out."""
    bl_index = blacklist_index(config.get("source_blacklist"))
    locked_id = str(config.get("stream", {}).get("locked_source_id") or "")
    visible = [s for s in ordered_stream_sources(config) if not is_blacklisted(s, bl_index)]
    for source in visible:
        source["locked"] = bool(locked_id and source.get("id") == locked_id)
    return [public_source_status(source, index=index, proc=proc) for index, source in enumerate(visible)]


def public_managed_sources(config, proc=None):
    """Return the single 'server-1' managed-HLS source descriptor (the default transcode output) for viewer clients."""
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
    """Evict viewer sessions whose last heartbeat is older than VIEWER_SESSION_TTL."""
    now = now or time.time()
    expired = [sid for sid, session in VIEWER_SESSIONS.items() if now - float(session.get("at") or 0) > VIEWER_SESSION_TTL]
    for sid in expired:
        VIEWER_SESSIONS.pop(sid, None)


def viewer_counts_snapshot():
    """Prune stale sessions, then return live viewer totals and per-source breakdown."""
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


# ---------------------------------------------------------------------------
# Viewer highscores / analytics
# Accrues watch time per client IP (stored only as a salted hash — never the raw
# IP), tags each with a stable codename + coarse geolocation, and exposes an
# anonymised leaderboard. Persisted to a JSON snapshot so it survives restarts.
# ---------------------------------------------------------------------------
VIEWER_STATS: dict[str, dict[str, Any]] = {}
SOURCE_QOE: dict[str, dict] = {}   # source_id -> {watch_ms, buffering_ms, stalls, viewers:set}
VIEWER_STATS_LOCK = asyncio.Lock()
#: Guards on a public, unauthenticated endpoint. The heartbeat is ~4KB with the
#: diagnostics block and a 60-event timeline; 64KB is generous headroom.
VIEWER_BODY_MAX_BYTES = 64 * 1024
#: The client beats every 15s. Below this, credit nothing.
VIEWER_MIN_BEAT_SECONDS = 5.0
VIEWER_ID_MAX_CHARS = 200
VIEWER_STATS_PATH = CONFIG_PATH.parent / "viewer_highscores.json"
VIEWER_STATS_DIRTY = False
VIEWER_WATCH_MAX_STEP = 90.0            # cap seconds credited per heartbeat gap
_GEO_CACHE: dict[str, dict] = {}
_GEO_INFLIGHT: set[str] = set()
_IP_HASH_SALT = "obbyviewer.v1"

_CODENAME_ADJ = [
    "Swift", "Silent", "Crimson", "Golden", "Shadow", "Iron", "Electric", "Frost",
    "Solar", "Rogue", "Mighty", "Cosmic", "Turbo", "Neon", "Velvet", "Savage",
    "Lucky", "Phantom", "Atomic", "Wild", "Blazing", "Midnight", "Emerald", "Thunder",
]
_CODENAME_NOUN = [
    "Falcon", "Tiger", "Viper", "Wolf", "Panther", "Cobra", "Eagle", "Rhino",
    "Jaguar", "Hawk", "Bear", "Fox", "Shark", "Lynx", "Raven", "Bison",
    "Otter", "Mantis", "Stallion", "Kraken", "Puma", "Falconer", "Drake", "Orca",
]


def _client_ip(request):
    """Best-effort client IP: first X-Forwarded-For hop, else the socket peer host."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return (request.client.host if request.client else "") or ""


def _ip_hash(ip):
    """Return a 16-char salted SHA-256 hash of an IP (the raw IP is never stored)."""
    return hashlib.sha256(f"{_IP_HASH_SALT}:{ip}".encode()).hexdigest()[:16]


def codename_for(ip_hash):
    """Derive a stable 'Adjective Noun' codename from an IP hash for anonymised leaderboards."""
    value = int(ip_hash[:8], 16)
    adj = _CODENAME_ADJ[value % len(_CODENAME_ADJ)]
    noun = _CODENAME_NOUN[(value // len(_CODENAME_ADJ)) % len(_CODENAME_NOUN)]
    return f"{adj} {noun}"


def mask_ip(ip):
    """Return a partially-masked display form of an IP (first octet/hextet + last octet for IPv4)."""
    if not ip:
        return "•"
    if ":" in ip:  # IPv6 — keep only the first hextet
        return ip.split(":", 1)[0] + ":••"
    parts = ip.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.•.•.{parts[3]}"
    return "•"


def _flag_emoji(cc):
    """Convert a 2-letter ISO country code into its regional-indicator flag emoji, or 🌐 when invalid."""
    if not cc or len(cc) != 2 or not cc.isalpha():
        return "🌐"
    return chr(0x1F1E6 + ord(cc[0].upper()) - 65) + chr(0x1F1E6 + ord(cc[1].upper()) - 65)


async def _resolve_geo(ip_hash, ip):
    """Resolve coarse geo for an IP via ip-api.com (cached; private IPs mapped to 'Local network') and apply it to the viewer's stats."""
    if not ip or ip in _GEO_CACHE or ip in _GEO_INFLIGHT:
        return
    try:
        ipobj = ipaddress.ip_address(ip)
        if ipobj.is_private or ipobj.is_loopback or ipobj.is_reserved:
            geo = {"country": "Local network", "cc": "", "region": "", "city": "", "flag": "🏠"}
            _GEO_CACHE[ip] = geo
            await _apply_geo(ip_hash, geo)
            return
    except ValueError:
        return
    _GEO_INFLIGHT.add(ip)
    geo = {"country": "", "cc": "", "region": "", "city": "", "flag": "🌐"}
    try:
        client = _HTTPX_CLIENT or httpx.AsyncClient(timeout=httpx.Timeout(6.0))
        resp = await client.get(f"http://ip-api.com/json/{ip}?fields=status,country,countryCode,regionName,city")
        data = resp.json()
        if data.get("status") == "success":
            cc = data.get("countryCode", "") or ""
            geo = {
                "country": data.get("country", "") or "",
                "cc": cc,
                "region": data.get("regionName", "") or "",
                "city": data.get("city", "") or "",
                "flag": _flag_emoji(cc),
            }
    except Exception:
        pass
    finally:
        _GEO_INFLIGHT.discard(ip)
    _GEO_CACHE[ip] = geo
    await _apply_geo(ip_hash, geo)


async def _apply_geo(ip_hash, geo):
    """Attach a resolved geo dict to an existing viewer stats record under the stats lock, marking it dirty."""
    global VIEWER_STATS_DIRTY
    async with VIEWER_STATS_LOCK:
        stats = VIEWER_STATS.get(ip_hash)
        if stats is not None:
            stats["geo"] = geo
            VIEWER_STATS_DIRTY = True


# Playback diagnostics reported per heartbeat by the player. Declarative rather
# than 25 more kwargs: each entry is (wire_name, kind, clamp_max), where kind is
#   "counter" -> a per-beat delta, summed; the client already resets its window
#   "gauge"   -> describes the beat itself; averaged via a sum/samples pair, so
#                a viewer who reports nothing does not drag the mean to zero
#
# The set is chosen around one question: "why did playback stop?". manifest_*
# exists because the 2026-08-22 freeze (nginx serving a 30s-stale playlist) was
# invisible to every server-side check -- the origin was correct the whole time,
# and only the client could see the manifest had stopped advancing.
QOE_COUNTERS = (
    ("stall_events", 1_000),
    ("stall_total_ms", 900_000),
    ("gap_jumps", 1_000),
    ("buffer_gap_events", 1_000),
    ("manifest_sequence_regressions", 1_000),
    ("segment_error_count", 10_000),
    ("level_switches", 10_000),
    ("fps_drop_events", 10_000),
    ("rate_warp_ms", 900_000),
)
QOE_GAUGES = (
    ("stall_longest_ms", 900_000.0),
    ("buffer_min_seconds", 3_600.0),
    ("live_latency_max_seconds", 3_600.0),
    ("latency_drift_seconds", 3_600.0),
    ("manifest_age_ms", 900_000.0),
    ("manifest_advance_rate", 100.0),
    ("manifest_jump_max_segments", 100_000.0),
    ("manifest_fetch_ms_max", 300_000.0),
    ("playback_rate_avg", 16.0),
    ("seek_range_span_seconds", 86_400.0),
    ("segment_ttfb_ms_p50", 300_000.0),
    ("segment_ttfb_ms_max", 300_000.0),
    ("bandwidth_estimate_bps", 10_000_000_000.0),
    ("dropped_frame_ratio", 1.0),
    ("corrupted_frames", 10_000_000.0),
)
#: Bound the event timeline a single heartbeat may contribute.
QOE_MAX_EVENTS = 60
QOE_EVENT_KIND_MAX = 40
QOE_EVENT_CARDINALITY = 60


def _qoe_counter_defaults():
    return {name: 0 for name, _ in QOE_COUNTERS}


def _qoe_gauge_defaults():
    out = {}
    for name, _ in QOE_GAUGES:
        out[f"{name}_sum"] = 0.0
        out[f"{name}_samples"] = 0
    return out


def record_source_qoe(source_id, ip_hash, credit_seconds, buffering_ms, stalls, label=None,
                      reattaches=0, live_latency_seconds=None, dropped_frames=0,
                      last_error=None, mirror_id=None, playback=None, events=None):
    """Accumulate per-source quality-of-experience from client heartbeats.

    reattaches and live_latency_seconds are the two that actually diagnose
    "it's skipping": a re-attach tears the pipeline down and restarts the
    playhead at live, so each one IS a visible skip, and latency says whether a
    viewer is riding the live edge or stuck far behind it. Both are computed in
    the browser; before they were reported these had to be reconstructed by
    counting init-segment refetches in the nginx access log.

    last_error and mirror_id say WHY and WHERE. Without them a re-attach count
    is a number with no cause attached, and since the two Cloudflare mirrors are
    the same vhost, a fault specific to one hostname is otherwise invisible.

    playback carries the QOE_COUNTERS/QOE_GAUGES block and events the ordered
    timeline behind it. Aggregates say a stall happened; only the ordering says
    what preceded it.
    """
    stats = SOURCE_QOE.setdefault(source_id, {
        "watch_ms": 0.0, "buffering_ms": 0.0, "stalls": 0, "viewers": set(),
        "reattaches": 0, "dropped_frames": 0, "latency_sum": 0.0, "latency_samples": 0,
        "errors": {}, "mirrors": {}, "event_kinds": {},
        **_qoe_counter_defaults(), **_qoe_gauge_defaults(),
    })
    # Older persisted snapshots predate these keys.
    for key, default in (("reattaches", 0), ("dropped_frames", 0), ("latency_sum", 0.0), ("latency_samples", 0)):
        stats.setdefault(key, default)
    for key, default in _qoe_counter_defaults().items():
        stats.setdefault(key, default)
    for key, default in _qoe_gauge_defaults().items():
        stats.setdefault(key, default)
    for key in ("errors", "mirrors", "event_kinds"):
        if not isinstance(stats.get(key), dict):
            stats[key] = {}
    stats["watch_ms"] += max(0.0, credit_seconds) * 1000.0
    stats["buffering_ms"] += max(0.0, min(float(buffering_ms or 0), 60_000.0))
    stats["stalls"] += max(0, int(stalls or 0))
    # Clamped: these arrive from the public internet and are deltas per heartbeat.
    stats["reattaches"] += max(0, min(int(reattaches or 0), 1000))
    stats["dropped_frames"] += max(0, min(int(dropped_frames or 0), 1_000_000))
    if live_latency_seconds is not None:
        try:
            latency = float(live_latency_seconds)
        except (TypeError, ValueError):
            latency = None
        if latency is not None and 0.0 <= latency <= 3600.0:
            stats["latency_sum"] += latency
            stats["latency_samples"] += 1
    # Every value below arrives from a public, unauthenticated endpoint, so each
    # one is coerced and clamped rather than trusted.
    block = playback if isinstance(playback, dict) else {}
    for name, cap in QOE_COUNTERS:
        try:
            value = int(float(block.get(name) or 0))
        except (TypeError, ValueError):
            continue
        stats[name] += max(0, min(value, cap))
    for name, cap in QOE_GAUGES:
        raw = block.get(name)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value) or not (-cap <= value <= cap):
            continue
        # sum/samples, not a running mean: a viewer that reports nothing must not
        # be counted as a zero.
        stats[f"{name}_sum"] += value
        stats[f"{name}_samples"] += 1
    if isinstance(events, list):
        kinds = stats["event_kinds"]
        for item in events[:QOE_MAX_EVENTS]:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "")[:QOE_EVENT_KIND_MAX]
            if not kind:
                continue
            if kind in kinds or len(kinds) < QOE_EVENT_CARDINALITY:
                kinds[kind] = kinds.get(kind, 0) + 1
    # Free-text from the public internet: clamp the value length and the number
    # of distinct keys, or a hostile client can grow this dict without bound.
    if last_error:
        errors = stats["errors"]
        key = str(last_error)[:120]
        if key in errors or len(errors) < 40:
            errors[key] = errors.get(key, 0) + 1
    if mirror_id:
        mirrors = stats["mirrors"]
        key = str(mirror_id)[:40]
        if key in mirrors or len(mirrors) < 20:
            mirrors[key] = mirrors.get(key, 0) + 1
    if label:
        stats["label"] = str(label)[:80]
    if ip_hash:
        viewers = stats["viewers"]
        if isinstance(viewers, set):
            viewers.add(ip_hash)


async def record_watch(ip_hash, ip, source_id, now_t, last_at, buffering_ms=0, stalls=0, source_label=None,
                       reattaches=0, live_latency_seconds=None, dropped_frames=0,
                       last_error=None, mirror_id=None, playback=None, events=None):
    """Credit watch time to a viewer (capped per heartbeat gap), update QoE, and kick off geo resolution for new/ungeo'd IPs."""
    global VIEWER_STATS_DIRTY
    credit = 0.0
    if last_at is not None:
        gap = now_t - float(last_at)
        if 0 < gap <= VIEWER_SESSION_TTL * 2:
            credit = min(gap, VIEWER_WATCH_MAX_STEP)
    record_source_qoe(source_id, ip_hash, credit, buffering_ms, stalls, label=source_label,
                      reattaches=reattaches, live_latency_seconds=live_latency_seconds,
                      dropped_frames=dropped_frames, last_error=last_error, mirror_id=mirror_id,
                      playback=playback, events=events)
    new_ip = False
    async with VIEWER_STATS_LOCK:
        stats: dict[str, Any]
        existing = VIEWER_STATS.get(ip_hash)
        if existing is None:
            new_ip = True
            stats = {
                "codename": codename_for(ip_hash),
                "ip_masked": mask_ip(ip),
                "total": 0.0,
                "by_source": {},
                "first": now_ms(),
                "last": now_ms(),
                "geo": _GEO_CACHE.get(ip),
            }
            VIEWER_STATS[ip_hash] = stats
        else:
            stats = existing
        stats["last"] = now_ms()
        if credit > 0:
            stats["total"] = float(stats.get("total", 0.0)) + credit
            stats["by_source"][source_id] = float(stats["by_source"].get(source_id, 0.0)) + credit
        if not stats.get("geo") and ip in _GEO_CACHE:
            stats["geo"] = _GEO_CACHE[ip]
        VIEWER_STATS_DIRTY = True
    if (new_ip or not (VIEWER_STATS.get(ip_hash) or {}).get("geo")) and ip:
        _spawn_background(_resolve_geo(ip_hash, ip))


async def viewer_highscores_snapshot(limit=25):
    """Build the anonymised analytics payload: viewer leaderboard, top/best sources, per-source QoE, and top countries."""
    async with VIEWER_STATS_LOCK:
        rows = [dict(s) for s in VIEWER_STATS.values()]
    rows.sort(key=lambda s: s.get("total", 0.0), reverse=True)
    leaderboard = []
    for index, stats in enumerate(rows[:limit]):
        by_source = stats.get("by_source", {})
        favorite = max(by_source.items(), key=lambda kv: kv[1])[0] if by_source else None
        geo = stats.get("geo") or {}
        location = ", ".join(part for part in (geo.get("region"), geo.get("country")) if part)
        leaderboard.append({
            "rank": index + 1,
            "codename": stats.get("codename"),
            "ip_masked": stats.get("ip_masked"),
            "watch_seconds": int(stats.get("total", 0.0)),
            "favorite_source": favorite,
            "flag": geo.get("flag") or "🌐",
            "location": location,
            "country": geo.get("country") or "",
            "first_seen": stats.get("first"),
            "last_seen": stats.get("last"),
        })
    source_totals: dict[str, float] = {}
    for stats in rows:
        for source_id, seconds in stats.get("by_source", {}).items():
            source_totals[source_id] = source_totals.get(source_id, 0.0) + seconds
    top_sources = sorted(source_totals.items(), key=lambda kv: kv[1], reverse=True)
    # Per-source QoE: smoothness = share of watch time NOT spent buffering (client-reported).
    source_performance: list[dict[str, Any]] = []
    for source_id, qoe in SOURCE_QOE.items():
        watch_ms = float(qoe.get("watch_ms", 0.0))
        buffering_ms = float(qoe.get("buffering_ms", 0.0))
        viewers = len(qoe.get("viewers") or ())
        buffer_ratio = (buffering_ms / watch_ms) if watch_ms > 0 else 0.0
        source_performance.append({
            "source_id": source_id,
            "label": qoe.get("label") or source_id,
            "watch_hours": round(watch_ms / 3_600_000.0, 2),
            "smoothness": round(max(0.0, min(1.0, 1.0 - buffer_ratio)) * 100.0, 1),
            "buffering_minutes": round(buffering_ms / 60_000.0, 1),
            "stalls": int(qoe.get("stalls", 0)),
            "reattaches": int(qoe.get("reattaches", 0)),
            "dropped_frames": int(qoe.get("dropped_frames", 0)),
            "avg_live_latency_seconds": (
                round(qoe["latency_sum"] / qoe["latency_samples"], 1)
                if qoe.get("latency_samples") else None
            ),
            # What viewers actually hit, most common first. A re-attach count on
            # its own says something is wrong; these say what.
            "top_errors": [
                {"error": err, "count": count}
                for err, count in sorted(
                    (qoe.get("errors") or {}).items(), key=lambda kv: kv[1], reverse=True
                )[:5]
            ],
            "by_mirror": dict(
                sorted((qoe.get("mirrors") or {}).items(), key=lambda kv: kv[1], reverse=True)
            ),
            # Counters as totals, gauges as means over the beats that actually
            # reported them. A gauge with no samples is null, not 0 -- "nobody
            # measured it" and "it measured zero" are different answers.
            **{name: int(qoe.get(name, 0) or 0) for name, _ in QOE_COUNTERS},
            **{
                name: (
                    round(qoe[f"{name}_sum"] / qoe[f"{name}_samples"], 3)
                    if qoe.get(f"{name}_samples") else None
                )
                for name, _ in QOE_GAUGES
            },
            "event_kinds": dict(
                sorted((qoe.get("event_kinds") or {}).items(), key=lambda kv: kv[1], reverse=True)[:15]
            ),
            "viewers": viewers,
        })
    source_performance.sort(key=lambda s: s["watch_hours"], reverse=True)
    best_sources = sorted(
        (s for s in source_performance if s["watch_hours"] >= 0.02),
        key=lambda s: (s["smoothness"], s["watch_hours"]), reverse=True,
    )[:5]
    country_totals: dict[str, dict] = {}
    for stats in rows:
        geo = stats.get("geo") or {}
        country = geo.get("country")
        if country:
            entry = country_totals.setdefault(country, {"country": country, "flag": geo.get("flag") or "🌐", "seconds": 0.0, "viewers": 0})
            entry["seconds"] += stats.get("total", 0.0)
            entry["viewers"] += 1
    top_countries = sorted(country_totals.values(), key=lambda c: c["seconds"], reverse=True)
    return {
        "ok": True,
        "updated_at": now_ms(),
        "viewers_tracked": len(rows),
        "total_watch_hours": round(sum(s.get("total", 0.0) for s in rows) / 3600.0, 1),
        "leaderboard": leaderboard,
        "top_sources": [{"source_id": sid, "watch_hours": round(sec / 3600.0, 2)} for sid, sec in top_sources[:8]],
        "source_performance": source_performance[:10],
        "best_sources": best_sources,
        "top_countries": [{"country": c["country"], "flag": c["flag"], "watch_hours": round(c["seconds"] / 3600.0, 2), "viewers": c["viewers"]} for c in top_countries[:8]],
    }


def load_viewer_stats():
    """Load persisted viewer stats and per-source QoE from the JSON snapshot (viewer sets rehydrated), defaulting to empty on any error."""
    global VIEWER_STATS, SOURCE_QOE
    try:
        with VIEWER_STATS_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        loaded = data.get("stats", {})
        if isinstance(loaded, dict):
            VIEWER_STATS = loaded
        source_qoe = data.get("source_qoe", {})
        if isinstance(source_qoe, dict):
            SOURCE_QOE = {
                sid: {**{k: v for k, v in q.items() if k != "viewers"}, "viewers": set(q.get("viewers") or ())}
                for sid, q in source_qoe.items()
            }
    except FileNotFoundError:
        VIEWER_STATS = {}
        SOURCE_QOE = {}
    except Exception as exc:
        logger.warning("viewer stats load failed: %s", exc)
        VIEWER_STATS = {}
        SOURCE_QOE = {}


async def flush_viewer_stats(force=False):
    """Atomically persist viewer stats + QoE (viewer sets serialized as sorted lists) when dirty or force=True."""
    global VIEWER_STATS_DIRTY
    async with VIEWER_STATS_LOCK:
        if not VIEWER_STATS_DIRTY and not force:
            return
        source_qoe = {
            sid: {**{k: v for k, v in q.items() if k != "viewers"}, "viewers": sorted(q.get("viewers") or ())}
            for sid, q in SOURCE_QOE.items()
        }
        payload = {"saved_at": now_ms(), "stats": VIEWER_STATS, "source_qoe": source_qoe}
        VIEWER_STATS_DIRTY = False
    try:
        tmp = VIEWER_STATS_PATH.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, VIEWER_STATS_PATH)
    except Exception as exc:
        logger.warning("viewer stats flush failed: %s", exc)


async def viewer_highscores(request):
    """Public GET /api/highscores handler: returns the anonymised leaderboard snapshot (clamped limit, CORS, short cache)."""
    cors = public_cors_headers()
    if request.method == "OPTIONS":
        return Response("", headers=cors)
    try:
        limit = max(1, min(100, int(request.query_params.get("limit", "25"))))
    except (TypeError, ValueError):
        limit = 25
    data = await viewer_highscores_snapshot(limit=limit)
    return JSONResponse(data, headers={**cors, "Cache-Control": "max-age=5"})


# ---------------------------------------------------------------------------
# Stream health scorer — sample HLS output over time into a failure decision.
# ---------------------------------------------------------------------------
@dataclass
class StreamHealthScorer:
    """Stateful health judge for the managed encode: accumulates timed HLS samples and emits a decision (healthy/failed/…)."""

    pid: int | None = None
    started_at: int | None = None
    last_sample_at: float = 0.0
    consecutive_bad_samples: int = 0
    consecutive_good_samples: int = 0
    previous_hls: dict = field(default_factory=dict)
    samples: deque[dict] = field(default_factory=lambda: deque(maxlen=90))
    last_assessment: dict | None = None
    # Wall time we last saw an ffmpeg child under the supervisor. The supervisor
    # relaunches ffmpeg itself on an upstream blip, and for those couple of
    # seconds there is legitimately no encoder and no fresh playlist — which
    # scores far below the failure threshold. Without remembering that an encoder
    # existed a moment ago, the watchdog confirms "failed" in ~4s and kills the
    # supervisor mid-recovery, turning a 2.5s blip into a ~10s outage.
    last_encoder_seen_at: float = 0.0

    def reset(self, pid=None, started_at=None):
        """Clear all accumulated sample state, optionally re-seeding the tracked pid/started_at for a new process."""
        self.pid = pid
        self.started_at = started_at
        self.last_sample_at = 0.0
        self.consecutive_bad_samples = 0
        self.consecutive_good_samples = 0
        self.previous_hls = {}
        self.samples.clear()
        self.last_assessment = None
        # A different managed process: the startup grace covers its first seconds,
        # and carrying the old one's encoder sighting over would be a lie.
        self.last_encoder_seen_at = 0.0

    def assess(self, config, proc, hls, force=False):
        """Score the current proc+HLS snapshot, update streak counters, and return the health assessment dict.

        Auto-resets on process change; throttles to health_sample_interval unless force=True; requires
        confirmed_failure_samples consecutive bad samples past min_assessment_seconds before deciding 'failed'.
        """
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

        # Age of the ffmpeg actually producing output, not of the supervisor that
        # outlives it across relaunches. Falls back to the supervisor's age while
        # no encoder child exists (which is itself what the score is judging).
        supervisor_age = float(proc.get("age") or 0.0)
        encoder_age = proc.get("encoder_age")
        elapsed = float(encoder_age) if encoder_age is not None else supervisor_age
        min_assessment = float(stream.get("min_assessment_seconds", 15))
        stale_seconds = float(stream.get("playlist_stale_seconds", 25))
        ramp_seconds = float(stream.get("failure_ramp_seconds", 60))
        success_threshold = float(stream.get("success_score_threshold", 180))
        failure_threshold = float(stream.get("failure_score_threshold", -120))
        confirmed_failure_samples = int(stream.get("confirmed_failure_samples", 2))

        # startup_grace_seconds was accepted by config and normalization but read
        # by nothing, so a supervisor that had launched but not yet produced a
        # playlist could be declared failed and killed while it was still coming
        # up. It gates on the SUPERVISOR's age deliberately: during the window
        # where ffmpeg does not exist yet there is no encoder age to gate on.
        startup_grace = float(stream.get("startup_grace_seconds", 25))
        within_startup_grace = supervisor_age < startup_grace
        if encoder_age is not None:
            self.last_encoder_seen_at = now
        elif (
            proc.get("managed")
            and self.last_encoder_seen_at
            and now - self.last_encoder_seen_at < startup_grace
        ):
            # Supervisor is up and had an encoder moments ago: it is relaunching
            # ffmpeg, not wedged. Judging this window is what made the watchdog
            # kill the very recovery it was waiting on.
            within_startup_grace = True

        score, evidence, reasons = score_stream_snapshot(proc, hls, self.previous_hls, elapsed, min_assessment, stale_seconds, ramp_seconds, recent_errors)
        bad_sample = elapsed >= min_assessment and score <= failure_threshold and not within_startup_grace
        good_sample = score >= success_threshold
        if bad_sample:
            self.consecutive_bad_samples += 1
            self.consecutive_good_samples = 0
        elif good_sample:
            self.consecutive_good_samples += 1
            self.consecutive_bad_samples = 0
        elif score > failure_threshold / 2:
            self.consecutive_bad_samples = 0

        if within_startup_grace and score <= failure_threshold:
            state = "assessing"
            level = "warn"
            decision = "assessing"
            message = f"Within the {startup_grace:.0f}s startup grace window; not judging the stream yet."
        elif elapsed < min_assessment:
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
    """Return up to `limit` ffmpeg error records logged within the last `seconds`."""
    cutoff = time.time() - seconds
    return [item for item in list(ERRORS) if item.get("ts", 0) / 1000 >= cutoff][-limit:]


def bounded_penalty(base, cap, ramp):
    """Ramp-scaled penalty (base*ramp) clamped to a maximum of cap."""
    return min(cap, base * ramp)


def score_stream_snapshot(proc, hls, previous_hls, elapsed, min_assessment, stale_seconds, ramp_seconds, recent_errors):
    """Compute a health score for one snapshot from ffmpeg-child presence, playlist freshness, HLS progress deltas, and recent errors.

    Returns (score, evidence, reasons). Penalties ramp up with elapsed time so early samples are judged leniently.
    """
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
    """Return a 0-100 confidence blended from elapsed time, sample count, signal strength, and streak length (capped at 85 pre-assessment)."""
    elapsed_score = min(45, (elapsed / max(min_assessment, 1.0)) * 45)
    sample_score = min(30, sample_count * 5)
    signal_score = min(25, abs(score) / 8)
    streak_score = min(15, max(bad_samples, good_samples) * 5)
    confidence = int(min(100, elapsed_score + sample_score + signal_score + streak_score))
    if elapsed < min_assessment:
        return min(85, confidence)
    return confidence


# ---------------------------------------------------------------------------
# Event log & auth guards — operator event feed plus session/origin checks.
# ---------------------------------------------------------------------------
_EVENT_LOG_LEVELS = {
    "ok": logging.INFO,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def event(message, level="info", extra=None):
    """Append an operator event to the EVENTS ring and queue it for ArangoDB; returns the created item."""
    item = {"ts": now_ms(), "level": level, "message": message, "extra": extra or {}}
    EVENTS.append(item)
    queue_arango_insert("events", item)
    # Also emit to journald. The EVENTS ring holds 300 entries and dies with the
    # process, and ArangoDB is not what you reach for at 3am — so without this a
    # restart leaves no record of WHY it restarted. Every operator decision in
    # this file (restart, watchdog action, source switch, stand-down) goes
    # through here, so this one line is the whole audit trail.
    logger.log(_EVENT_LOG_LEVELS.get(str(level).lower(), logging.INFO), "event: %s", _redact_message(str(message)))
    return item


def require_auth(request):
    """True only if the request supplies the configured dashboard session_token (via header or cookie); fails closed when unset."""
    config = load_config()
    token = config.get("dashboard", {}).get("session_token", "")
    if not token:
        # Fail closed: an unset session_token must lock the guarded surface, not
        # open it. Previously this returned True, exposing every write/admin
        # endpoint whenever the token was blank.
        logger.error("require_auth denied: no dashboard.session_token configured")
        return False
    supplied = request.headers.get("x-obbystreams-token", "") or request.cookies.get("obbystreams_token", "")
    if not supplied:
        return False
    return secrets.compare_digest(supplied, token)


def guarded(handler):
    """Decorator that wraps a route handler with same-origin enforcement (for writes) and session-token auth."""
    async def wrapped(request):
        """Enforce origin/token guards, then delegate to the wrapped handler (or return 401/403)."""
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            has_header_token = bool(request.headers.get("x-obbystreams-token", "").strip())
            if not trusted_request_origin(request) and not has_header_token:
                return JSONResponse({"ok": False, "error": "forbidden origin"}, status_code=403)
        if not require_auth(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        return await handler(request)
    return wrapped


async def parse_json_body(request):
    """Parse a JSON request body into a dict; returns {} for empty bodies, raises ValueError for invalid/non-object JSON."""
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
    """POST /api/auth/login: verify the dashboard password (same-origin only) and set the session-token cookie."""
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


# ---------------------------------------------------------------------------
# ArangoDB — HTTP client, fire-and-forget insert queue, and retrying worker.
# ---------------------------------------------------------------------------
def arango_auth_header(config):
    """Build the HTTP Basic Authorization header from the arangodb username/password."""
    arango = config.get("arangodb", {})
    raw = f"{arango.get('username')}:{arango.get('password')}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


async def arango_request(method, path, payload=None):
    """Issue an authenticated request to the configured ArangoDB database; returns None when Arango is disabled, raises on HTTP error."""
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
    """Insert a document into a collection, swallowing all errors (returns None on failure)."""
    try:
        return await arango_request("POST", f"/_api/document/{collection}", doc)
    except Exception:
        return None


def queue_arango_insert(collection, doc):
    """Enqueue a non-blocking insert for the background worker; counts a dropped write if the queue is full."""
    global ARANGO_QUEUE
    if ARANGO_QUEUE is None:
        return
    item = {"collection": collection, "doc": doc, "attempt": 1}
    try:
        ARANGO_QUEUE.put_nowait(item)
    except asyncio.QueueFull:
        RUNTIME["arango_dropped_writes"] += 1


async def arango_worker_loop():
    """Background task: drain the insert queue, POSTing each doc with exponential-backoff retries up to ARANGO_RETRY_MAX_ATTEMPTS."""
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
    """GET /api/arango: report ArangoDB connectivity and version (always 200; connected=False carries the error)."""
    try:
        data = await arango_request("GET", "/_api/version")
        return JSONResponse({"ok": True, "connected": True, "version": data})
    except Exception as exc:
        return JSONResponse({"ok": True, "connected": False, "error": str(exc)})


# ---------------------------------------------------------------------------
# Process discovery & operator-stop state — find/kill stray encodes, read stop switch.
# ---------------------------------------------------------------------------
def stream_processes():
    """Scan the process table for unmanaged obbystreams/ufc encode processes (excluding this app and its managed child tree)."""
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
    """Terminate (then SIGKILL after a 2s grace) every stray encode found by stream_processes; return the list killed."""
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
    _gone, alive = psutil.wait_procs(procs, timeout=2)
    for proc in alive:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.kill()
    return killed


def operator_stopped(config):
    """True when an operator has manually Stopped the cockpit.

    This is the persisted master kill switch (``stream.operator_stopped`` in the
    config YAML). While set, the watchdog, both scrapers, and every auto-start
    path stay idle until an explicit Start/Restart clears it — the stop survives
    supervisor ticks AND full service/host restarts.
    """
    return bool(config.get("stream", {}).get("operator_stopped", False))


def set_operator_stopped(config, value, reason=""):
    """Persist the operator Stop/Start intent into the config YAML.

    Mutates ``config`` in place and writes it via :func:`save_config` so the
    intent is durable. Callers are limited to the explicit
    ``/api/stream/{start,stop,restart}`` endpoints and the auto-scheduler
    (:func:`schedule_stop_stream` / :func:`schedule_start_stream`); no
    process-lifecycle side effect ever calls this.

    ``reason`` records *who* stopped it — :class:`StopReason` ``manual`` or
    ``schedule`` — so the cockpit can tell a deliberate shutdown apart from a
    scheduled standby. It is cleared whenever the stream is started.
    """
    stream = config.setdefault("stream", {})
    stream["operator_stopped"] = bool(value)
    stream["stop_reason"] = str(reason or "") if value else ""
    save_config(config)


def stop_reason(config):
    """Why the stream is currently down ("manual", "schedule", or "" when running)."""
    if not operator_stopped(config):
        return ""
    return str(config.get("stream", {}).get("stop_reason") or "")


def _reconcile_operator_stopped(config):
    """Stamp the currently-persisted operator Stop into ``config`` before saving.

    Long-running writers (e.g. the private-IPTV refresh) snapshot the config, do
    tens of seconds of network/ffprobe work, then save it back. Without this an
    operator Stop that landed during that window would be clobbered by the stale
    snapshot. Call this IMMEDIATELY before ``save_config`` with no ``await`` in
    between so the read+write is atomic w.r.t. the (coroutine-scheduled) endpoints.
    """
    fresh = load_config(fresh=True)
    stream = config.setdefault("stream", {})
    stream["operator_stopped"] = operator_stopped(fresh)
    stream["stop_reason"] = stop_reason(fresh)


def should_watchdog_restart_exited_process(config, desired_state):
    """Whether the watchdog may auto-restart a managed process that has exited.

    Returns False when the operator has Stopped the stream, when auto-recovery is
    disabled, or when there are no links to start — so a manual Stop is never
    silently undone.
    """
    stream = config.get("stream", {})
    return (
        desired_state == "running"
        and not operator_stopped(config)
        and stream.get("auto_recover", True)
        and stream.get("auto_restart_on_exit", True)
        and bool(effective_stream_links(config))
    )


def boot_stream_desired_state(config):
    """Safe process intent after a service/host restart.

    Auto-schedule owns starts when enabled, so boot into standby and let its
    first event-aware tick choose verified links. This closes the restart race
    where the watchdog could launch stale configured links before ESPN context
    had loaded.
    """
    schedule_enabled = ScheduleSettings.from_config(config.get("schedule")).enabled
    return "stopped" if operator_stopped(config) or schedule_enabled else "running"


# ---------------------------------------------------------------------------
# HLS output & process metrics — read the transcode's playlist/segment state.
# ---------------------------------------------------------------------------
def safe_stat_size(path):
    """Return path's size in bytes, or 0 if it cannot be stat'd."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def safe_stat_mtime(path):
    """Return path's mtime, or None if it cannot be stat'd."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def classify_stream_log(line):
    """Classify a wrapper/ffmpeg log line into a level: error, warn, info, or debug (by keyword heuristics)."""
    lowered = line.lower()
    if "starting" in lowered or "stream commander" in lowered or "status:" in lowered:
        return "info"
    if "ffmpeg:" in lowered or any(token in lowered for token in ("error", "failed", "invalid", "timed out", "timeout", "403", "404", "500")):
        return "error"
    if "ffmpeg exited" in lowered or "restart" in lowered or "weak stream" in lowered or "every link failed" in lowered:
        return "warn"
    return "debug"


def _read_playlist(path):
    """Read an m3u8 file into a list of lines, returning [] if missing or unreadable."""
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _parse_playlist_lines(lines):
    """Parse m3u8 lines into (media_sequence, target_duration, segment_names, segment_durations, nested_media_playlists)."""
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
    """Inspect the output dir and return a metrics dict: playlist/DASH freshness, segment counts/bytes, encode rate, and live lag."""
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
        candidate_paths = [output_dir / name for name in media_playlist_names]
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
    # Encode rate (the "1.0x / 0.9x" factor). Prefer ffmpeg's OWN reported speed=,
    # which the wrapper publishes to .encode-progress.json each stats block; fall
    # back to deriving it from HLS segment cadence when that file is missing/stale.
    now_wall = time.time()
    encode_rate = None
    encode_rate_source = None
    try:
        prog_path = output_dir / ".encode-progress.json"
        if prog_path.exists():
            with prog_path.open("r", encoding="utf-8") as prog_file:
                prog = json.load(prog_file)
            speed_raw = str(prog.get("speed", "")).strip().rstrip("xX")
            if (now_wall - float(prog.get("at", 0))) <= 12 and speed_raw and speed_raw.upper() != "N/A":
                encode_rate = round(float(speed_raw), 2)
                encode_rate_source = "ffmpeg"
    except Exception:
        pass
    live_lag_seconds = None
    playlist_seg_mtimes = sorted(
        m for m in (safe_stat_mtime(output_dir / name) for name in playlist_segment_names[-9:]) if m
    )
    if encode_rate is None and len(playlist_seg_mtimes) >= 2 and len(segment_durations) >= 2:
        wall_span = playlist_seg_mtimes[-1] - playlist_seg_mtimes[0]
        content_seconds = sum(segment_durations[-(len(playlist_seg_mtimes)):][1:])
        if wall_span > 0 and content_seconds > 0:
            encode_rate = round(content_seconds / wall_span, 2)
            encode_rate_source = "derived"
    if playlist_seg_mtimes:
        live_lag_seconds = round(max(0.0, now_wall - playlist_seg_mtimes[-1]), 2)
    elif segment_mtimes:
        live_lag_seconds = round(max(0.0, now_wall - max(segment_mtimes)), 2)
    return {
        "output_dir": str(output_dir),
        "encode_rate": encode_rate,
        "encode_rate_source": encode_rate_source,
        "live_lag_seconds": live_lag_seconds,
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


_PROC_METRICS_CACHE: dict[str, Any] = {"at": 0.0, "data": None}
_PROC_METRICS_TTL = 2.0


def process_metrics():
    """Return the managed encode's live process metrics (pid, age, cpu, rss, child processes), served from a 2s TTL cache."""
    # Cached: this does a recursive psutil children() scan (full /proc walk) that
    # otherwise ran once per viewer per SSE/poll tick and pegged the single event
    # loop, starving proxy_hls async reads. Shared TTL cache keeps it O(1/2s)
    # regardless of viewer count.
    global PROCESS, STARTED_AT
    _pm_now = time.monotonic()
    if _PROC_METRICS_CACHE["data"] is not None and (_pm_now - _PROC_METRICS_CACHE["at"]) < _PROC_METRICS_TTL:
        return _PROC_METRICS_CACHE["data"]
    pid = PROCESS.pid if PROCESS and PROCESS.poll() is None else None
    data = {"managed": bool(pid), "pid": pid, "started_at": STARTED_AT, "age": None, "cpu": None, "rss": None, "children": []}
    if not pid:
        _PROC_METRICS_CACHE["data"] = data
        _PROC_METRICS_CACHE["at"] = _pm_now
        return data
    try:
        proc = psutil.Process(pid)
        data["age"] = max(0, time.time() - proc.create_time())
        data["cpu"] = proc.cpu_percent(interval=0.0)
        data["rss"] = proc.memory_info().rss
        data["cmd"] = " ".join(proc.cmdline())
        children = []
        encoder_age = None
        for c in proc.children(recursive=True):
            child_age = None
            with contextlib.suppress(psutil.Error):
                child_age = max(0.0, time.time() - c.create_time())
            children.append({
                "pid": c.pid, "name": c.name(), "cpu": c.cpu_percent(interval=0.0),
                "rss": c.memory_info().rss, "age": child_age,
            })
            if child_age is not None and "ffmpeg" in (c.name() or "") and (encoder_age is None or child_age < encoder_age):
                encoder_age = child_age
        data["children"] = children
        # The supervisor outlives the ffmpeg processes it launches, so its age is
        # the wrong clock for "has this encode had time to produce output yet".
        # Scoring on it meant that ~150s after boot the failure ramp was pinned at
        # maximum, and the ~2.5s gap while the supervisor relaunched ffmpeg scored
        # far below the failure threshold — so the watchdog killed the supervisor
        # mid-recovery and turned a 2.5s blip into a ~10s outage.
        data["encoder_age"] = encoder_age
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    _PROC_METRICS_CACHE["data"] = data
    _PROC_METRICS_CACHE["at"] = _pm_now
    return data


def stream_health(config, proc, hls, force=False):
    """Convenience wrapper delegating to the module-global StreamHealthScorer.assess."""
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


# ---------------------------------------------------------------------------
# NVIDIA GPU telemetry — run/parse/analyze nvidia-smi for encoder health.
# ---------------------------------------------------------------------------
def parse_smi_csv(text, fields):
    """Parse nvidia-smi CSV output into a list of {field: cell} dicts, padding/truncating each row to the field list."""
    rows = []
    reader = csv.reader(io.StringIO(text or ""))
    for raw in reader:
        if not any(cell.strip() for cell in raw):
            continue
        padded = (raw + [""] * len(fields))[: len(fields)]
        rows.append({field: cell.strip() for field, cell in zip(fields, padded, strict=False)})
    return rows


def parse_nvidia_gpu_csv(text):
    """Parse --query-gpu CSV into per-GPU dicts with typed fields and derived memory/power utilization percentages."""
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
    """Parse the NVENC encoder-stats CSV (session count, average fps/latency) into per-GPU-index dicts."""
    return [
        {
            "index": smi_int(row.get("index")),
            "encoder_session_count": smi_int(row.get("encoder_session_count")),
            "encoder_average_fps": smi_int(row.get("encoder_average_fps")),
            "encoder_average_latency_ms": smi_int(row.get("encoder_average_latency_ms")),
        }
        for row in parse_smi_csv(text, NVIDIA_ENCODER_FIELDS)
    ]


def parse_nvidia_process_csv(text):
    """Parse the --query-compute-apps CSV into per-process dicts (gpu_uuid, pid, name, used memory), skipping rows without a pid."""
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
    """Parse `nvidia-smi pmon` whitespace output into per-process rows with sm/mem/enc/dec utilization percentages."""
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
    """Merge compute-app and pmon process rows (keyed by pid), tag each with gpu_index and an is_ffmpeg flag, sorted by gpu/pid."""
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
    """Run `nvidia-smi args` with a timeout, returning a result dict (command, returncode, stdout, stderr, elapsed_ms); never raises."""
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
    """Return the trailing max_chars of a stripped string (the tail is the useful part of command output)."""
    text = str(text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def nvidia_command_summary(result, include_stdout=False):
    """Summarize an nvidia-smi result for the API (command, returncode, elapsed, truncated stderr; stdout on error or when asked)."""
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
    """Return the max of the non-None values, or None if there are none."""
    filtered = [value for value in values if value is not None]
    return max(filtered) if filtered else None


def sum_or_none(values):
    """Return the sum (rounded to 1dp) of the non-None values, or None if there are none."""
    filtered = [value for value in values if value is not None]
    return round(sum(filtered), 1) if filtered else None


def analyze_nvidia_smi(gpus, processes, commands):
    """Reduce parsed GPU/process data into an availability verdict, level, message, diagnosis, and summary (hot/mem/NVENC checks)."""
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
    """Run the full nvidia-smi query suite (gpus, encoder, compute-apps, pmon), merge and analyze it into one telemetry payload."""
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
    """GET /api/nvidia-smi: return GPU telemetry from a shared ~5s cache, collecting off-thread on miss and recording it to Arango."""
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


# ---------------------------------------------------------------------------
# HTTP route handlers — cockpit status, config, sources, and links APIs.
# ---------------------------------------------------------------------------
def status_payload():
    """Assemble the full cockpit status document (config, process/HLS/health, sources, viewers, logs, runtime) and record it to Arango."""
    config = load_config()
    proc = process_metrics()
    hls = hls_metrics(config)
    health_doc = stream_health(config, proc, hls)
    update_private_probe_runtime(config, private_probe_budget(config, proc=proc, health_doc=health_doc))
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
        "health": health_doc,
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
            "operator_stopped": operator_stopped(config),
            "stop_reason": stop_reason(config),
            "configured_link_count": len(configured_links),
            "configured_source_count": len(sources),
            "auto_public_source_count": len(auto_links),
            "active_link_pool_count": len(active_links),
            "proxy_cache": _PROXY_CACHE.stats(),
        },
        "private_iptv": private_iptv_public_runtime(),
        "schedule": schedule_snapshot(),
    }
    queue_arango_insert("metrics", {"ts": now_ms(), "payload": payload})
    return payload


async def status(request):
    """GET /api/status (guarded): return the full cockpit status payload."""
    return JSONResponse(status_payload())


async def list_sources(request):
    """GET /api/sources (guarded): return ingest source statuses with live viewer counts."""
    config = load_config()
    return JSONResponse({"ok": True, "sources": source_statuses(config, process_metrics()), "viewers": viewer_counts_snapshot()})


async def viewer_counts(request):
    """GET/POST /api/viewers: return live viewer counts; POST also registers a viewer heartbeat and credits watch time."""
    cors = public_cors_headers()
    if request.method == "OPTIONS":
        return Response("", headers=cors)
    if request.method == "POST":
        # This route is public and unauthenticated, and the diagnostics payload
        # made it meaningfully larger. Bound the body before parsing it, and the
        # beat rate before crediting it.
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > VIEWER_BODY_MAX_BYTES:
            return JSONResponse({"ok": False, "error": "payload too large"}, status_code=413, headers=cors)
        try:
            body = await parse_json_body(request)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400, headers=cors)
        session_id = str(body.get("session_id") or body.get("viewer_id") or secrets.token_urlsafe(16))[:VIEWER_ID_MAX_CHARS]
        source_id = str(body.get("source_id") or "server-1")[:VIEWER_ID_MAX_CHARS]
        client_ip = _client_ip(request)
        ip_hash = _ip_hash(client_ip) if client_ip else None
        now_t = time.time()
        async with VIEWER_LOCK:
            prune_viewer_sessions()
            previous = VIEWER_SESSIONS.get(session_id)
            last_at = float(previous.get("at", 0.0)) if previous else None
            # The client beats every 15s. Anything much faster is either a bug or
            # someone inflating their own numbers; count the session but do not
            # let it accumulate QoE. Rejecting outright would hide real viewers.
            too_fast = last_at is not None and (now_t - last_at) < VIEWER_MIN_BEAT_SECONDS
            VIEWER_SESSIONS[session_id] = {"source_id": source_id, "at": now_t, "ip_hash": ip_hash}
            counts = viewer_counts_snapshot()
        if ip_hash and not too_fast:
            await record_watch(
                ip_hash, client_ip, source_id, now_t, last_at,
                buffering_ms=body.get("buffering_ms", 0), stalls=body.get("stalls", 0),
                source_label=body.get("source_label"),
                reattaches=body.get("reattaches", 0),
                live_latency_seconds=body.get("live_latency_seconds"),
                dropped_frames=body.get("dropped_frames", 0),
                last_error=body.get("last_error"),
                mirror_id=body.get("mirror_id"),
                playback={name: body.get(name) for name, _ in (*QOE_COUNTERS, *QOE_GAUGES)},
                events=body.get("events"),
            )
        return JSONResponse({"ok": True, "session_id": session_id, "viewers": counts}, headers=cors)
    async with VIEWER_LOCK:
        counts = viewer_counts_snapshot()
    return JSONResponse({"ok": True, "viewers": counts}, headers=cors)


async def health(request):
    """GET /api/health (public): report readiness with per-check details; returns 503 when the managed stream is down/unconfigured/failed."""
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


_MPD_AST_RE = re.compile(r'availabilityStartTime="([^"]+)"')
_MPD_REP_RE = re.compile(r'<Representation id="(\d+)".*?</Representation>', re.S)
_MPD_TIMESCALE_RE = re.compile(r'timescale="(\d+)"')
# Continuation entries carry no t=, so t must be optional or the timeline is
# undercounted (this is what made audio look 28s out of sync during the audit).
_MPD_S_RE = re.compile(r'<S(?:\s+t="(\d+)")?\s+d="(\d+)"(?:\s+r="(-?\d+)")?\s*/>')


def dash_timeline_drift_seconds(output_dir):
    """Seconds by which the MPD's advertised live edge leads wall clock.

    ffmpeg stamps availabilityStartTime when the muxer starts, then drains the
    source's backlog as fast as it arrives (observed 1.8x for the first seconds).
    That pushes media time permanently ahead of AST, so the manifest advertises
    segments that are not yet "available" by a player's own arithmetic — pure
    added latency that grows with the size of the startup burst and is invisible
    unless something measures it. Positive = advertising future content.
    """
    manifest = Path(output_dir) / "ufc.mpd"
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    ast_match = _MPD_AST_RE.search(text)
    rep_match = _MPD_REP_RE.search(text)
    if not ast_match or not rep_match:
        return None
    try:
        ast = datetime.fromisoformat(ast_match.group(1).replace("Z", "+00:00"))
    except ValueError:
        return None
    rep = rep_match.group(0)
    timescale_match = _MPD_TIMESCALE_RE.search(rep)
    entries = _MPD_S_RE.findall(rep)
    if not timescale_match or not entries:
        return None
    timescale = int(timescale_match.group(1)) or 1
    first_t = int(entries[0][0] or 0)
    total = 0
    for _, duration, repeat in entries:
        total += int(duration) * (int(repeat or 0) + 1)
    edge = ast.timestamp() + (first_t + total) / timescale
    return round(edge - time.time(), 2)


def _openmetrics_lines(name, help_text, metric_type, samples):
    """Render one OpenMetrics family; samples is a list of (labels|None, value)."""
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}"]
    for labels, value in samples:
        if value is None:
            continue
        try:
            rendered = float(value)
        except (TypeError, ValueError):
            continue
        lines.append(f"{name}{{{labels}}} {rendered}" if labels else f"{name} {rendered}")
    return lines


async def metrics(request):
    """GET /metrics (guarded): OpenMetrics exposition of stream, encode and viewer state.

    Guarded deliberately. nginx proxies this vhost to the public internet, and the
    counters below describe upstream sources and viewer activity.
    """
    config = load_config()
    proc = process_metrics()
    hls = hls_metrics(config)
    health_doc = stream_health(config, proc, hls, force=True)
    viewers = viewer_counts_snapshot()
    output_dir = Path(config.get("stream", {}).get("output_dir", "/var/www/live.obnoxious.lol/stream"))

    out: list[str] = []
    out += _openmetrics_lines("obbystreams_up", "1 when a managed ffmpeg process is running.", "gauge",
                              [(None, 1 if proc.get("managed") else 0)])
    out += _openmetrics_lines("obbystreams_health_score", "Stream health score; failure threshold is negative.", "gauge",
                              [(None, health_doc.get("score"))])
    out += _openmetrics_lines("obbystreams_encode_rate_ratio", "Encoded content seconds per wall second; 1.0 keeps up.", "gauge",
                              [(None, hls.get("encode_rate"))])
    out += _openmetrics_lines("obbystreams_live_lag_seconds", "Age of the newest published segment.", "gauge",
                              [(None, hls.get("live_lag_seconds"))])
    out += _openmetrics_lines("obbystreams_playlist_age_seconds", "Age of the HLS media playlist.", "gauge",
                              [(None, hls.get("playlist_age"))])
    out += _openmetrics_lines("obbystreams_dash_timeline_drift_seconds",
                              "Seconds the MPD's advertised live edge leads wall clock; grows with the startup burst.", "gauge",
                              [(None, dash_timeline_drift_seconds(output_dir))])
    out += _openmetrics_lines("obbystreams_segments_published", "Segments currently listed in the playlist.", "gauge",
                              [(None, hls.get("segments"))])
    out += _openmetrics_lines("obbystreams_viewers", "Currently tracked viewers.", "gauge",
                              [(None, (viewers or {}).get("total"))])

    for key, help_text in (
        ("stream_starts", "Managed stream starts since boot."),
        ("stream_restarts", "Managed stream restarts since boot."),
        ("watchdog_restarts", "Restarts initiated by the watchdog since boot."),
        ("start_failures", "Failed start attempts since boot."),
        ("arango_dropped_writes", "Analytics writes dropped because the queue was full."),
        ("arango_write_failures", "Analytics writes that failed to persist."),
    ):
        out += _openmetrics_lines(f"obbystreams_{key}_total", help_text, "counter", [(None, RUNTIME.get(key))])

    proxy_cache = RUNTIME.get("proxy_cache") or {}
    for key in ("hits", "misses", "evictions", "upstream_errors"):
        out += _openmetrics_lines(f"obbystreams_proxy_cache_{key}_total", f"HLS proxy cache {key}.", "counter",
                                  [(None, proxy_cache.get(key))])

    # Client-reported playback quality, the only signal that reflects what a
    # viewer actually experienced rather than what the origin emitted.
    smoothness, stalls, reattaches, latency = [], [], [], []
    stall_seconds, gap_jumps, regressions, advance = [], [], [], []
    for source_id, qoe in SOURCE_QOE.items():
        label = str(qoe.get("label") or source_id).replace("\\", "\\\\").replace('"', '\\"')
        selector = f'source_id="{str(source_id)[:120]}",label="{label[:120]}"'
        watch_ms = float(qoe.get("watch_ms", 0.0))
        buffering_ms = float(qoe.get("buffering_ms", 0.0))
        if watch_ms > 0:
            smoothness.append((selector, round(max(0.0, 1.0 - buffering_ms / watch_ms) * 100.0, 2)))
        stalls.append((selector, qoe.get("stalls", 0)))
        reattaches.append((selector, qoe.get("reattaches", 0)))
        if qoe.get("latency_samples"):
            latency.append((selector, round(qoe["latency_sum"] / qoe["latency_samples"], 2)))
        stall_seconds.append((selector, round(float(qoe.get("stall_total_ms", 0) or 0) / 1000.0, 2)))
        gap_jumps.append((selector, qoe.get("gap_jumps", 0) or 0))
        regressions.append((selector, qoe.get("manifest_sequence_regressions", 0) or 0))
        if qoe.get("manifest_advance_rate_samples"):
            advance.append((selector, round(
                qoe["manifest_advance_rate_sum"] / qoe["manifest_advance_rate_samples"], 3)))
    out += _openmetrics_lines("obbystreams_source_smoothness_percent",
                              "Share of client watch time not spent buffering.", "gauge", smoothness)
    out += _openmetrics_lines("obbystreams_source_stalls_total", "Client-reported stalls.", "counter", stalls)
    out += _openmetrics_lines("obbystreams_source_reattaches_total",
                              "Client player teardown/re-attach cycles; each one is a visible skip.", "counter", reattaches)
    out += _openmetrics_lines("obbystreams_source_client_latency_seconds",
                              "Mean client-reported distance behind the live edge.", "gauge", latency)
    # The four that answer "is playback actually working for viewers". Deliberately
    # not all 24 -- /metrics is for alerting, /api/highscores carries the full set.
    out += _openmetrics_lines("obbystreams_source_stall_seconds_total",
                              "Client-reported time with playback frozen.", "counter", stall_seconds)
    out += _openmetrics_lines("obbystreams_source_gap_jumps_total",
                              "Playhead nudges over a buffer hole; each is a micro-skip.", "counter", gap_jumps)
    out += _openmetrics_lines("obbystreams_source_manifest_regressions_total",
                              "Clients saw the media sequence go BACKWARDS -- a cache is serving stale playlists.",
                              "counter", regressions)
    out += _openmetrics_lines("obbystreams_source_manifest_advance_rate",
                              "Media-seconds the manifest advertised per wall-second, as clients saw it. 1.0 is healthy.",
                              "gauge", advance)

    out.append("# EOF")
    return Response("\n".join(out) + "\n", media_type="text/plain; version=0.0.4; charset=utf-8")


async def get_config(request):
    """GET /api/config (guarded): return the redacted public config."""
    return JSONResponse({"ok": True, "config": public_config(load_config())})


async def put_config(request):
    """PUT /api/config (guarded): validate and apply config edits, persist, and hot-restart the encode if stream-affecting keys changed."""
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
    if "source_blacklist" in body:
        if not isinstance(body["source_blacklist"], list):
            return JSONResponse({"ok": False, "error": "source_blacklist must be an array"}, status_code=400)
        config["source_blacklist"] = normalize_blacklist(body["source_blacklist"])
    if "private_iptv" in body:
        if not isinstance(body["private_iptv"], dict):
            return JSONResponse({"ok": False, "error": "private_iptv must be an object"}, status_code=400)
        existing_private = config.get("private_iptv", {})
        merged_private = {**existing_private, **body["private_iptv"]}
        # Redacted payloads from the UI must not erase live credentials.
        if isinstance(merged_private.get("cookies"), dict):
            merged_private["cookies"] = {
                key: existing_private.get("cookies", {}).get(key, value) if value == "***" else value
                for key, value in merged_private["cookies"].items()
            }
        config["private_iptv"] = normalize_private_iptv(merged_private)
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


async def private_iptv_status(request):
    """GET /api/private-iptv (guarded): return the private-IPTV runtime state plus its redacted config."""
    return JSONResponse({"ok": True, "private_iptv": private_iptv_public_runtime(), "config": public_config(load_config()).get("private_iptv", {})})


async def private_iptv_refresh(request):
    """POST /api/private-iptv/refresh (guarded): trigger an on-demand private-IPTV scrape (optionally forcing playback probes)."""
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    result = await refresh_private_iptv_sources(reason="api", force_probe=bool(body.get("force_probe")))
    status = 200 if result.get("ok") else 500
    return JSONResponse(result, status_code=status)


async def private_iptv_control(request):
    """Control only the scraper lifecycle; never stop or restart ffmpeg."""
    global PRIVATE_IPTV_TASK
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    action = str(body.get("action") or "").lower().strip()
    if action not in {"pause", "resume", "stop", "restart"}:
        return JSONResponse({"ok": False, "error": "action must be pause, resume, stop, or restart"}, status_code=400)
    config = load_config(fresh=True)
    private_cfg = config.setdefault("private_iptv", {})
    if action == "pause":
        private_cfg["paused"] = True
    elif action == "stop":
        private_cfg["enabled"] = False
        private_cfg["paused"] = False
    else:
        private_cfg["enabled"] = True
        private_cfg["paused"] = False
    _reconcile_operator_stopped(config)
    save_config(config)
    if action == "restart":
        if PRIVATE_IPTV_TASK and not PRIVATE_IPTV_TASK.done():
            PRIVATE_IPTV_TASK.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await PRIVATE_IPTV_TASK
        PRIVATE_IPTV_TASK = asyncio.create_task(private_iptv_loop())
    state = "paused" if action == "pause" else "stopped" if action == "stop" else "running"
    PRIVATE_IPTV_RUNTIME.update({"enabled": bool(private_cfg.get("enabled")), "state": state, "message": f"Private IPTV automation {state}; live ffmpeg was not touched."})
    event(f"private IPTV automation {action}", "ok")
    return JSONResponse({"ok": True, "action": action, "private_iptv": private_iptv_public_runtime()})


async def add_link(request):
    """POST /api/links (guarded): add a new ingest source URL, persist, and hot-restart the running encode."""
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
    """POST /api/links/remove (guarded): remove an ingest source by url or id, persist, and hot-restart the running encode."""
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
_M3U_ATTR_RE = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')
_PRIVATE_IPTV_DOWNLOAD_RE = re.compile(r'<a[^>]+id=["\']m3uDownloadBtn["\'][^>]+href=["\']([^"\']+)["\']', re.IGNORECASE)
_PRIVATE_IPTV_DATE_RE = re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}\b", re.IGNORECASE)
_PRIVATE_IPTV_NUMERIC_DATE_RE = re.compile(r"\b(?:20\d{2})[ ._/-](\d{1,2})[ ._/-](\d{1,2})\b")
# Bare MM.DD / MM/DD (no year), e.g. "(07.11 5:00PM ET)" / "(7.11 9:00 PM ET)".
# Times use ':' so they won't match here.
_PRIVATE_IPTV_SHORT_DATE_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})\b(?!\d)")
# Clock time in a title, e.g. "5:00PM ET", "9:00 PM". Assumed US Eastern.
_PRIVATE_IPTV_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([AaPp][Mm])\b")
# A named matchup in a title ("ANKALAEV VS. GUSKOV"). An entry that names a bout
# has already told us which card it is; if that did not match the tracked event,
# it is someone else's card, not a generic slot we can fall back on.
_PRIVATE_IPTV_VS_RE = re.compile(r"\b[a-z]{3,}\s+vs\.?\s+[a-z]{3,}", re.IGNORECASE)

PRIVATE_IPTV_RUNTIME: dict[str, Any] = {
    "enabled": False,
    "state": "idle",
    "last_checked_at": None,
    "last_changed_at": None,
    "playlist_url": "",
    "playlist_entries": 0,
    "candidate_count": 0,
    "accepted_count": 0,
    "active_source_ids": [],
    "message": "Private IPTV automation has not run yet.",
    "reasons": [],
    "next_check_at": None,
    "connection_limit": 2,
    "stream_uses_private_slot": False,
    "probe_allowed": True,
    "probe_skipped_reason": "",
    "last_probe_mode": "none",
}


# ---------------------------------------------------------------------------
# Private-IPTV scraper — authenticated provider playlist → scored/probed sources.
# ---------------------------------------------------------------------------
def private_soursignal_links(config):
    """Return the enabled private-IPTV soursignal source URLs currently in the config (used to gauge upstream slot usage)."""
    private_cfg = config.get("private_iptv", {})
    sources = normalize_sources(config.get("stream", {}).get("sources", []), config.get("stream", {}).get("links", []))
    links = [source.get("url") for source in sources if source.get("enabled", True) and is_private_soursignal_source(source, private_cfg)]
    if not links:
        links = [url for url in effective_stream_links(config) if is_soursignal_url(url)]
    return normalize_links(links)


def active_event_context():
    """The card the auto-schedule is tracking, or None outside an event window."""
    return ACTIVE_EVENT_CONTEXT


def live_stream_is_event_matched(config, context=None):
    """Whether the live ingest pool actually belongs to the tracked card.

    ffmpeg reporting frames says a feed is *working*, not that it is the right
    fight. With no source tagged for tonight's card the encode is, by
    construction, carrying something else — the exact state that went unnoticed
    for a whole event on 2026-08-01.
    """
    context = active_event_context() if context is None else context
    if context is None:
        return True
    return bool(event_source_links(config, context.event_id))


def live_stream_is_high_grade(config, context=None, now=None, actual_links=None):
    """Whether ffmpeg actually carries an exact/dated, deeply probed source."""
    context = active_event_context() if context is None else context
    if context is None:
        return False
    urls = set(event_source_links(config, context.event_id, context=context, now=now))
    restrict_to_actual = actual_links is not None
    if actual_links is None and PROCESS and PROCESS.poll() is None:
        actual_links = MANAGED_LINKS
        restrict_to_actual = True
    actual = set(actual_links or ())
    return any(
        source.get("url") in urls
        and (not restrict_to_actual or source.get("url") in actual)
        and source.get("match_confidence") in {"exact", "dated"}
        and int(source.get("probe_score") or 0) >= 80
        for source in ordered_stream_sources(config)
    )


def sources_predate_current_segment(config, context=None, now=None):
    """True when the card moved to a new segment after these sources were picked.

    Each broadcast segment is a different provider channel, so a source list
    chosen during the early prelims is stale the moment the main card opens even
    though the encode is still perfectly healthy.
    """
    context = active_event_context() if context is None else context
    if context is None or not context.segments:
        return False
    moment = now or datetime.now(UTC)
    segment = context.current_segment(moment)
    if segment is None:
        return False
    discovered = [
        int(source.get("discovered_at") or 0)
        for source in config.get("stream", {}).get("sources", [])
        if source.get("enabled", True) and str(source.get("event_id") or "") == str(context.event_id)
    ]
    if not discovered:
        return False
    return max(discovered) < int(segment[0].timestamp() * 1000)


def private_probe_budget(config, proc=None, health_doc=None, force_probe=False, context=None):
    """Decide whether an ffprobe of private-IPTV candidates is allowed now, respecting the provider connection_limit and reserving a spare slot while a healthy private stream is live.

    The spare-slot reservation is suspended while a tracked card is in its
    window: during those few hours, confirming the feed is the right one is
    worth more than holding a connection back.
    """
    private_cfg = config.get("private_iptv", {})
    proc = proc or process_metrics()
    context = active_event_context() if context is None else context
    in_event_window = bool(context is not None and context.active)
    stream_uses_private_slot = bool(proc.get("managed") and private_soursignal_links(config))
    connection_limit = int(private_cfg.get("connection_limit", 2))
    reserve_spare = bool(private_cfg.get("reserve_spare_when_streaming", True)) and not in_event_window
    decision = str((health_doc or {}).get("decision") or "").lower()
    allowed = True
    reason = ""
    if force_probe:
        reason = "forced by operator"
    elif connection_limit <= 1 and stream_uses_private_slot:
        allowed = False
        reason = "private stream is already using the only allowed upstream slot"
    elif reserve_spare and stream_uses_private_slot and decision not in {"failed", "degraded", "stopped"}:
        allowed = False
        reason = "private stream is live; spare upstream slot reserved"
    return {
        "connection_limit": connection_limit,
        "stream_uses_private_slot": stream_uses_private_slot,
        "probe_allowed": allowed,
        "probe_skipped_reason": reason,
        "health_decision": decision or "unknown",
        "in_event_window": in_event_window,
    }


def should_protect_live_private_stream(config, budget, force_probe=False, context=None, mismatch_confirmed=False):
    """Keep a working fight feed pinned during background refreshes.

    A provider scan must never consume another playback connection, rewrite the
    source list, or restart ffmpeg while the managed private feed is producing
    usable output.  An explicit forced operator refresh is the only override;
    confirmed degraded/failed states remain eligible for recovery.

    Health alone is not enough to earn protection, though. A feed that cannot be
    the tracked card (``mismatch_confirmed``), or one picked before the card
    moved to its next segment, is rescanned however well it is encoding —
    protecting those is what pinned last week's channel through a whole event.
    """
    private_cfg = config.get("private_iptv", {})
    decision = str((budget or {}).get("health_decision") or "").lower()
    context = active_event_context() if context is None else context
    if mismatch_confirmed or (context is not None and context.active and sources_predate_current_segment(config, context)):
        return False
    if context is not None and context.active and not live_stream_is_high_grade(config, context):
        return False
    return bool(
        private_cfg.get("protect_live_stream_on_refresh", True)
        and not force_probe
        and (budget or {}).get("stream_uses_private_slot")
        and decision not in {"failed", "degraded", "stopped"}
    )


def reset_switch_state(event_id):
    """Start the per-card switch budget over when the tracked card changes."""
    if SOURCE_SWITCH_STATE.get("event_id") == event_id:
        return
    SOURCE_SWITCH_STATE.update(
        {
            "event_id": event_id,
            "switches": 0,
            "last_switch_at": 0.0,
            "mismatch_samples": 0,
            "acquire_attempts": 0,
            "last_reasons": [],
            "last_error": "",
            "selected_confidence": None,
        }
    )


# How stale the wrapper's published link may be before we stop trusting it. It
# rewrites .encode-progress.json on every ffmpeg stats block (~1s), so anything
# older than this means the encode is not currently running.
ACTIVE_LINK_MAX_AGE_SECONDS = 15.0

#: Pause between killing the encode and starting the next one.
#:
#: The provider enforces connection_limit=2 and does not free a slot the instant
#: ffmpeg dies. Restarting immediately therefore raced the teardown and lost:
#: both restarts on 2026-08-22 hit "Server returned 429 Too Many streams" on the
#: first attempt and only recovered when the watchdog fired ~27s later, turning a
#: ~20s planned outage into ~60s. Waiting a moment first is strictly faster than
#: being rate-limited and retried.
PROVIDER_DRAIN_SECONDS = 3.0


def active_encode_link(config):
    """The link URL ffmpeg is actually ingesting, or "" if unknown.

    The wrapper rotates links internally on failure, so the app cannot infer this
    from config order -- it only ever knew "we launched with this list". Assuming
    link 1 is what made a restart abandon a healthy feed: on 2026-08-22 a 19
    minute old encode at 1.00x was restarted and re-entered at a link that had
    already failed 20 minutes earlier, while the link it had been happily using
    was still in the pool at position 3.
    """
    output_dir = config.get("stream", {}).get("output_dir")
    if not output_dir:
        return ""
    try:
        path = Path(output_dir) / ".encode-progress.json"
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return ""
    if time.time() - float(payload.get("at", 0) or 0) > ACTIVE_LINK_MAX_AGE_SECONDS:
        return ""
    url = str(payload.get("link_url") or "")
    # Free ride: the wrapper publishes how smoothly this link is delivering in the
    # same file, and this is the one function that reads it on a regular cadence.
    record_link_quality(url, payload.get("read_lag_per_min"))
    return url


def links_with_active_first(links, active_url):
    """Reorder links so the currently-working one is tried first after a restart.

    The wrapper always starts at links[0]. Without this, every restart -- watchdog,
    source refresh, segment transition -- discards which link was known to work and
    rediscovers it by burning through the dead ones ahead of it, each failure
    costing viewers another few seconds of black.
    """
    if not active_url or active_url not in links:
        return links
    return [active_url] + [link for link in links if link != active_url]


#: Per-link upstream delivery quality, keyed by link URL:
#:   {url: {"lag_per_min": float, "samples": int, "updated_at": ms}}
#: Kept in memory: link URLs carry rotating tokens and go stale between cards, so
#: persisting them would mostly preserve dead keys.
LINK_QUALITY: dict[str, dict] = {}
#: A feed lagging more than this is publishing in clumps rather than smoothly.
#: Measured 2026-08-22: a good feed sits at 0.4s/min, a bad one at 19.5s/min, and
#: the bad one produced viewer-visible freezes while every server-side health
#: check read perfectly healthy (speed=1x, 0 dropped frames).
LINK_LAG_BAD_PER_MIN = 6.0
#: Below this many samples the reading is noise, not a verdict.
LINK_QUALITY_MIN_SAMPLES = 3
LINK_QUALITY_EMA_WEIGHT = 0.15


def record_link_quality(url, lag_per_min):
    """Remember how smoothly a link delivered, so restarts can prefer the good ones."""
    if not url or lag_per_min is None:
        return
    try:
        value = float(lag_per_min)
    except (TypeError, ValueError):
        return
    if not math.isfinite(value) or value < 0:
        return
    entry = LINK_QUALITY.setdefault(url, {"lag_per_min": value, "samples": 0, "updated_at": 0})
    # Exponential moving average. The weight is the whole trade-off: too fast and
    # one bad minute condemns a good feed and triggers a restart nobody needed
    # (each restart costs every viewer a re-attach); too slow and a feed that
    # degrades mid-card is still preferred while it freezes people. At 0.15 a
    # single blip from 0.4 -> 30 lands at 4.8 (under the bad threshold), while two
    # consecutive bad readings clear it.
    entry["lag_per_min"] = (
        entry["lag_per_min"] * (1 - LINK_QUALITY_EMA_WEIGHT) + value * LINK_QUALITY_EMA_WEIGHT
        if entry["samples"] else value
    )
    entry["samples"] += 1
    entry["updated_at"] = now_ms()


def link_quality_rank(url):
    """Sort key: lower is smoother. Unmeasured links sort between good and bad."""
    entry = LINK_QUALITY.get(url or "")
    if not entry or entry.get("samples", 0) < LINK_QUALITY_MIN_SAMPLES:
        # Never seen: optimistic enough to get tried, pessimistic enough that a
        # link known to be smooth wins.
        return LINK_LAG_BAD_PER_MIN / 2.0
    return float(entry.get("lag_per_min", 0.0))


def links_by_quality(links):
    """Order links smoothest-first, preserving the original order within a tie.

    Delivery smoothness is invisible to the health scorer -- a bursty feed keeps
    ffmpeg at speed=1x with zero dropped frames while publishing segments in
    clumps, which starves player buffers and reads to a viewer as the stream
    freezing for a second or two. This is the only place that preference is
    expressed.
    """
    return sorted(links, key=lambda url: (link_quality_rank(url), links.index(url)))


def links_with_active_last(links, active_url):
    """Reorder links so the one that just failed is tried last.

    The mirror image of links_with_active_first, for the watchdog: there the
    active link is by definition the one whose failure triggered the restart, so
    leading with it wastes a whole attempt. Observed on 2026-08-22 at 14:45 --
    the watchdog fired on a stale playlist, restarted onto the same dead link 1,
    failed again 37s later, then walked 2 and 3 before settling on link 4.
    """
    if not active_url or active_url not in links or len(links) < 2:
        return links
    return [link for link in links if link != active_url] + [active_url]


def source_switch_allowed(config, *, force=False, stream_running=True):
    """Whether the live encode may be restarted onto a different source now.

    Every switch costs viewers a few seconds of black, so mid-fight flapping is
    worse than a slightly worse feed. Returns (allowed, reason).
    """
    if not stream_running:
        return True, ""
    private_cfg = config.get("private_iptv", {})
    cooldown = float(private_cfg.get("switch_cooldown_seconds", 300) or 0)
    max_switches = int(private_cfg.get("max_switches_per_card", 6))
    last = float(SOURCE_SWITCH_STATE.get("last_switch_at") or 0.0)
    # force skips the COOLDOWN — a segment transition or a confirmed wrong-event
    # feed must not wait out a 5 minute timer. It must not skip the BUDGET: that
    # is the backstop against a flapping upstream restarting the encode without
    # limit, and every restart is visible to every viewer. Previously force
    # short-circuited both, so max_switches_per_card constrained nothing in
    # exactly the cases that restart the stream most.
    if SOURCE_SWITCH_STATE.get("switches", 0) >= max_switches:
        return False, f"source switch budget for this card is spent ({max_switches})"
    if force:
        return True, ""
    elapsed = time.monotonic() - last
    if last and elapsed < cooldown:
        return False, f"source switch cooldown: {int(cooldown - elapsed)}s remaining"
    return True, ""


def record_source_switch():
    SOURCE_SWITCH_STATE["switches"] = int(SOURCE_SWITCH_STATE.get("switches", 0)) + 1
    SOURCE_SWITCH_STATE["last_switch_at"] = time.monotonic()
    SOURCE_SWITCH_STATE["mismatch_samples"] = 0


def update_private_probe_runtime(config, budget=None, mode=None):
    """Publish the current probe budget (and optional probe mode) into the PRIVATE_IPTV_RUNTIME status dict."""
    budget = budget or private_probe_budget(config)
    PRIVATE_IPTV_RUNTIME.update(
        {
            "connection_limit": budget.get("connection_limit"),
            "stream_uses_private_slot": budget.get("stream_uses_private_slot"),
            "probe_allowed": budget.get("probe_allowed"),
            "probe_skipped_reason": budget.get("probe_skipped_reason") or "",
        }
    )
    if mode:
        PRIVATE_IPTV_RUNTIME["last_probe_mode"] = mode


def private_iptv_cookie_header(config):
    """Build a Cookie header string from the private_iptv cookies map."""
    cookies = config.get("cookies") or {}
    parts = [f"{key}={value}" for key, value in cookies.items() if key and value]
    return "; ".join(parts)


def private_iptv_request_headers(config, referer=None):
    """Build the outbound request headers for provider fetches: configured headers plus Cookie, Referer, and a default User-Agent."""
    headers = dict(config.get("headers") or {})
    if referer and "Referer" not in headers:
        headers["Referer"] = referer
    cookie = private_iptv_cookie_header(config)
    if cookie:
        headers["Cookie"] = cookie
    if "User-Agent" not in headers:
        headers["User-Agent"] = _SCRAPE_UA
    return headers


async def private_iptv_fetch_text(url, config, referer=None):
    """Fetch a provider/playlist URL as text via the shared httpx client, falling back to a curl subprocess on HTTP/decoding errors."""
    headers = private_iptv_request_headers(config, referer=referer)
    client = _HTTPX_CLIENT
    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=4.0), follow_redirects=True)
        close_client = True
    try:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.text
    except (httpx.HTTPError, httpx.DecodingError):
        curl_args = ["curl", "-fsSL", "--compressed", "--max-time", "18", "-L"]
        for name, value in headers.items():
            curl_args += ["-H", f"{name}: {value}"]
        curl_args.append(url)
        proc = await asyncio.create_subprocess_exec(*curl_args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=22)
        if proc.returncode:
            detail = (stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"private IPTV fetch failed: {detail or proc.returncode}") from None
        return stdout.decode("utf-8", errors="replace")
    finally:
        if close_client:
            await client.aclose()


def extract_private_iptv_playlist_url(provider_html, provider_url, configured_url=""):
    """Return the m3u playlist URL: an explicit configured_url wins, else the m3uDownloadBtn href scraped from provider_html."""
    if valid_stream_url(configured_url):
        return configured_url
    match = _PRIVATE_IPTV_DOWNLOAD_RE.search(provider_html or "")
    if not match:
        return ""
    return urljoin(provider_url, html.unescape(match.group(1).strip()))


def parse_m3u_entries(text):
    """Parse an M3U playlist into a list of {title, attrs, url} entries (EXTINF attrs lower-cased and HTML-unescaped)."""
    entries = []
    pending = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            attrs = {key.lower(): html.unescape(value) for key, value in _M3U_ATTR_RE.findall(line)}
            title = line.rsplit(",", 1)[-1].strip() if "," in line else attrs.get("tvg-name", "")
            pending = {"title": html.unescape(title), "attrs": attrs, "url": ""}
            continue
        if line.startswith("#"):
            continue
        if not valid_stream_url(line):
            pending = None
            continue
        if pending is None:
            pending = {"title": "", "attrs": {}, "url": line}
        else:
            pending["url"] = line
        entries.append(pending)
        pending = None
    return entries


def private_iptv_entry_text(entry):
    """Flatten an m3u entry's title, tvg/group attrs, and URL into one searchable text blob for keyword scoring."""
    attrs = entry.get("attrs") or {}
    parts = [
        entry.get("title", ""),
        attrs.get("tvg-name", ""),
        attrs.get("group-title", ""),
        attrs.get("tvg-id", ""),
        entry.get("url", ""),
    ]
    return " ".join(str(part or "") for part in parts)


def private_iptv_now(config):
    """Return the current datetime in the configured private_iptv timezone (falling back to UTC if unknown)."""
    try:
        tz = ZoneInfo(config.get("timezone") or "Canada/Pacific")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    return datetime.now(tz)


def infer_private_iptv_event_date(text, now):
    """Best-effort parse of an event date from title text (today/tonight, month-name, numeric, or bare MM.DD), anchored to `now`; None if absent."""
    lowered = text.lower()
    if "today" in lowered or "tonight" in lowered:
        return now
    # Month-name date first ("Jul 11", "JUL 11").
    match = _PRIVATE_IPTV_DATE_RE.search(text)
    if match:
        for fmt in ("%b %d", "%B %d"):
            with contextlib.suppress(ValueError):
                parsed = datetime.strptime(match.group(0), fmt)
                return now.replace(month=parsed.month, day=parsed.day)
    # Year-prefixed numeric ("2026-07-11").
    match = _PRIVATE_IPTV_NUMERIC_DATE_RE.search(text)
    if match:
        month, day = int(match.group(1)), int(match.group(2))
        with contextlib.suppress(ValueError):
            return now.replace(month=month, day=day)
    # Bare MM.DD / MM/DD ("(07.11 5:00PM ET)") — the provider's dominant format.
    match = _PRIVATE_IPTV_SHORT_DATE_RE.search(text)
    if match:
        month, day = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            with contextlib.suppress(ValueError):
                return now.replace(month=month, day=day)
    return None


def infer_private_iptv_slot_start(text, now, context=None):
    """Best-effort US-Eastern start time for this event portion so the managed
    stream can follow the live phase. An explicit '(7:00 PM ET)' wins; otherwise
    infer from the phase keyword (early prelims 5pm / prelims 7pm / main card 9pm).
    Returns an ET-aware datetime, or None.

    When the auto-schedule is tracking a card, ``context`` carries ESPN's real
    per-segment start times and those replace the phase guesses — the hardcoded
    5/7/9pm ET defaults are wrong for every European and APAC card (a 13:00Z
    prelims block would otherwise read as eight hours away and be demoted)."""
    try:
        et = ZoneInfo("America/New_York")
    except Exception:
        return None
    now_et = now.astimezone(et)
    lowered = text.lower()
    match = _PRIVATE_IPTV_TIME_RE.search(text)
    if match:
        hour = int(match.group(1)) % 12
        if match.group(3).lower() == "pm":
            hour += 12
        minute = int(match.group(2))
    elif context is not None and context.segments:
        segment = event_context_segment_for(context, lowered)
        return segment[0].astimezone(et) if segment else None
    elif "early prelim" in lowered:
        hour, minute = 17, 0
    elif "prelim" in lowered:
        hour, minute = 19, 0
    elif "main card" in lowered or re.search(r"\bvs?\.?\b", lowered):
        hour, minute = 21, 0
    else:
        return None
    with contextlib.suppress(Exception):
        return now_et.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return None


def event_context_segment_for(context, lowered_text):
    """Match a provider title's phase wording to one of the card's real segments.

    Falls back to the last segment (the main card) for a title that names no
    phase at all, which is how the provider labels its marquee feed.
    """
    by_label = {label.lower(): (start, label) for start, label in context.segments}

    def pick(*labels):
        for label in labels:
            if label in by_label:
                return by_label[label]
        return None

    if "early prelim" in lowered_text:
        return pick("early prelims", "prelims")
    if "prelim" in lowered_text:
        return pick("prelims", "early prelims")
    if "main card" in lowered_text or re.search(r"\bvs?\.?\b", lowered_text):
        return pick("main card")
    return None


def score_private_iptv_entry(entry, config, now=None, context=None):
    """Score one m3u entry for fight-day relevance (event identity, keywords, event group/slot, main-event, date window, live phase) → (score, reasons).

    ``context`` is the tracked card (an :class:`~obbyschedule.EventContext`) when
    the auto-schedule is running. Naming one of tonight's fighters is by far the
    strongest evidence a channel is the right one, so it outweighs every generic
    signal combined.
    """
    now = now or private_iptv_now(config)
    text = private_iptv_entry_text(entry)
    lowered = text.lower()
    score = 0
    reasons = []
    if context is not None:
        matched, hits = context.matches(text)
        if matched:
            score += 60
            reasons.append(f"event match:{'/'.join(hits[:3])}")
    for keyword in config.get("keywords") or []:
        if keyword.lower() in lowered:
            score += 35 if keyword.lower() in {"ufc", "mma"} else 20
            reasons.append(f"keyword:{keyword}")
    group_title = (entry.get("attrs") or {}).get("group-title", "").lower()
    if "ppv" in group_title or "live event" in group_title:
        # The provider's live event feeds all live in the "PPV Live Events" group;
        # weight it strongly so a clear UFC entry clears the threshold even without
        # a parseable date (content is verified later by the ffprobe tester).
        score += 25
        reasons.append("event group")
    if re.search(r"\b(?:ppv|live)\s*(?:event\s*)?\d{1,2}\b", lowered):
        score += 15
        reasons.append("event slot")
    # Headline / main-event feeds ("... vs ...", "Main Card") carry no "prelims"
    # keyword bonus, so weight them so the marquee fight is never buried under the
    # prelims feeds when max_candidates is reached.
    if "prelim" not in lowered and ("main card" in lowered or re.search(r"\bvs?\.?\b", lowered)):
        score += 20
        reasons.append("main event")
    for keyword in config.get("reject_keywords") or []:
        if keyword.lower() in lowered:
            score -= 45
            reasons.append(f"reject:{keyword}")
    event_date = infer_private_iptv_event_date(text, now)
    if event_date:
        delta_hours = abs((event_date.date() - now.date()).days) * 24
        if delta_hours <= float(config.get("date_window_hours", 30)):
            score += 18
            reasons.append("date window")
        else:
            score -= 30
            reasons.append("stale/future date")
    # Event-phase awareness: strongly prefer the portion that is live NOW, and
    # demote portions that haven't started (they show a countdown/ads), so the
    # managed stream auto-follows early prelims -> prelims -> main card.
    slot_start = infer_private_iptv_slot_start(text, now, context=context)
    if slot_start is not None:
        delta_min = (now.astimezone(slot_start.tzinfo) - slot_start).total_seconds() / 60.0
        if delta_min < -10:
            score -= 40
            reasons.append("slot upcoming")
        elif delta_min <= 150:
            score += 25
            reasons.append("slot live")
        else:
            score += 5
            reasons.append("slot earlier")
    if re.search(r"\b(no event|no scheduled event)\b", lowered):
        score -= 100
    if re.search(r"\b(24/7|classic|replay)\b", lowered):
        score -= 30
    if not valid_stream_url(entry.get("url")):
        score -= 100
        reasons.append("invalid url")
    return score, reasons


def event_entry_is_plausible(entry, reasons, now, context):
    """Whether a playlist entry could plausibly be the tracked card's feed.

    Accepts either a positive identity match (a fighter surname or the event
    number) or, for the many generically-titled event slots the provider ships
    ("PPV 07 | MAIN CARD"), a date that lands on the card itself. Everything
    else — including a perfectly well-formed UFC channel for a different card —
    is out.
    """
    if any(reason.startswith("event match:") for reason in reasons):
        return True
    event_date = infer_private_iptv_event_date(private_iptv_entry_text(entry), now)
    if event_date is None:
        # No date and no name: only useful when it names no *other* card either,
        # which is the case for the numbered generic slots.
        return not _PRIVATE_IPTV_VS_RE.search(private_iptv_entry_text(entry))
    card_dates = {
        segment[0].astimezone(now.tzinfo).date()
        for segment in context.segments
    } or {now.date()}
    return event_date.date() in card_dates


def select_private_iptv_candidates(entries, config, now=None, blacklist=None, context=None, rejected=None):
    """Score and select fight-day private IPTV candidates.

    ``config`` is the ``private_iptv`` sub-config. ``blacklist`` (raw list or a
    precomputed :func:`blacklist_index` set) drops blocked entries up front so
    they never reach the expensive ffprobe stage or get re-selected each cycle.
    ``context`` is the tracked card; when present, an entry must identify *that*
    card (by fighter/event term, or failing that by carrying the card's own
    date) or it is rejected outright. ``rejected`` collects near-miss entries
    for the cockpit so "why is nothing on air?" is answerable.
    """
    now = now or private_iptv_now(config)
    bl_index = blacklist if isinstance(blacklist, set) else blacklist_index(blacklist or [])
    scored = []
    for index, entry in enumerate(entries):
        # Persistent blacklist: a blocked stream can never be re-selected.
        if bl_index and is_blacklisted(entry, bl_index):
            continue
        score, reasons = score_private_iptv_entry(entry, config, now=now, context=context)
        if context is not None and not event_entry_is_plausible(entry, reasons, now, context):
            # The Aug-1 failure in one line: a channel titled for last week's
            # card names none of tonight's fighters and carries the wrong date,
            # so it can no longer be selected however "UFC" it looks.
            if rejected is not None and any(r.startswith("keyword:") for r in reasons):
                rejected.append(
                    {
                        "title": entry.get("title"),
                        "score": score,
                        "reason": f"does not identify {context.short_name}",
                    }
                )
            continue
        required_keywords = config.get("required_keywords") or ["ufc"]
        if not any(re.search(rf"\b{re.escape(keyword.lower())}\b", private_iptv_entry_text(entry).lower()) for keyword in required_keywords):
            continue
        # Require an actual fight keyword (ufc/mma/prelims/...) so the group/slot/
        # main-event bonuses can't drag unrelated PPV entries (other sports) over
        # the threshold.
        if not any(r.startswith("keyword:") for r in reasons):
            continue
        # Recall over precision at selection time: keep dateless entries (many valid
        # event feeds omit a parseable date) and drop only clearly out-of-window ones.
        # The ffprobe tester downstream verifies which actually carry video.
        if "stale/future date" in reasons:
            continue
        if score >= int(config.get("min_score", 70)):
            scored.append({"entry": entry, "score": score, "reasons": reasons, "index": index})
    scored.sort(key=lambda item: (-item["score"], item["index"]))
    return scored[: int(config.get("max_candidates", 12))]


def parse_hls_urls(text, base_url):
    """Extract non-comment URLs from an m3u8 body, resolving each against base_url."""
    urls = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(urljoin(base_url, line))
    return urls


def looks_like_html(body):
    """Heuristic: True if the leading bytes of body look like an HTML document (doctype/<html>/<body>)."""
    sample = body.lstrip()[:256].lower()
    return sample.startswith((b"<!doctype html", b"<html")) or b"<body" in sample


async def fetch_small_head(url, headers, timeout=10.0):
    """GET the first ~128KB of url (via a Range header) and return (status, content_type, body) for cheap content probing."""
    client = _HTTPX_CLIENT
    close_client = False
    request_headers = dict(headers or {})
    request_headers.setdefault("Range", "bytes=0-131071")
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=4.0), follow_redirects=True)
        close_client = True
    try:
        response = await client.get(url, headers=request_headers, timeout=timeout)
        return response.status_code, response.headers.get("content-type", ""), response.content[:128_000]
    finally:
        if close_client:
            await client.aclose()


async def ffprobe_video(url, config, timeout=12.0):
    """Real content test: ffprobe the URL (following soursignal's 302 -> raw-TS CDN)
    and report the first video stream. Uses one upstream connection, so callers must
    hold PRIVATE_PROBE_LOCK to stay within the provider connection_limit."""
    headers = private_iptv_request_headers(config)
    ua = headers.get("User-Agent", _SCRAPE_UA)
    hdr_lines = "".join(f"{k}: {v}\r\n" for k, v in headers.items() if k.lower() in ("referer", "cookie"))
    cmd = [
        "ffprobe", "-v", "error", "-user_agent", ua,
        *(["-headers", hdr_lines] if hdr_lines else []),
        "-analyzeduration", "4000000", "-probesize", "4000000",
        "-show_entries", "stream=codec_type,codec_name,width,height",
        "-of", "json", url,
    ]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        if proc is not None:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.communicate()
        return {"video": False, "reason": "ffprobe timeout"}
    except Exception as exc:
        return {"video": False, "reason": f"ffprobe error:{exc}"[:80]}
    try:
        data = json.loads(out or b"{}")
    except Exception:
        data = {}
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and stream.get("codec_name"):
            return {"video": True, "codec": stream.get("codec_name"), "width": stream.get("width"), "height": stream.get("height")}
    tail = (err or b"").decode("utf-8", "replace").strip().splitlines()
    return {"video": False, "reason": (tail[-1] if tail else "no video stream")[:80]}


async def assess_playback_candidate(url, config, headers=None, deep=True):
    """Probe a candidate URL for real playable content — ffprobe for soursignal TS feeds, else HLS playlist/segment inspection — returning {ok, score, reasons, resolved_url}."""
    headers = headers or private_iptv_request_headers(config)
    reasons = []
    score = 0
    if "soursignal.com" in urlparse(url).netloc.lower() and ".m3u8" not in url.lower():
        # soursignal 302-redirects to a raw MPEG-TS CDN (not an HLS playlist), so
        # playlist-parsing probes can't see the content. Decode real video with
        # ffprobe instead; retry once because a live TS can start mid-packet.
        ff_timeout = max(float(config.get("probe_timeout_seconds", 10)), 12.0)
        info = await ffprobe_video(url, config, timeout=ff_timeout)
        if not info.get("video"):
            info = await ffprobe_video(url, config, timeout=ff_timeout)
        if info.get("video"):
            res = f"{info.get('width')}x{info.get('height')}"
            return {"ok": True, "score": 95, "reasons": [f"ffprobe video {info.get('codec')} {res}"], "resolved_url": None}
        return {"ok": False, "score": -80, "reasons": [f"ffprobe:{info.get('reason')}"], "resolved_url": None}
    try:
        status, content_type, body = await fetch_small_head(url, headers, timeout=float(config.get("probe_timeout_seconds", 10)))
    except Exception as exc:
        return {"ok": False, "score": -100, "reasons": [f"probe error:{exc}"], "resolved_url": None}
    if status >= 400:
        return {"ok": False, "score": -80, "reasons": [f"http {status}"], "resolved_url": None}
    if looks_like_html(body):
        return {"ok": False, "score": -60, "reasons": ["html response"], "resolved_url": None}
    text = body.decode("utf-8", errors="replace")
    is_playlist = "#EXTM3U" in text[:2048] or "mpegurl" in content_type.lower() or url.split("?")[0].endswith(".m3u8")
    if not is_playlist:
        return {"ok": False, "score": -20, "reasons": ["not hls"], "resolved_url": None}
    score += 30
    nested = parse_hls_urls(text, url)
    media_segments = [item for item in nested if not item.split("?", 1)[0].endswith(".m3u8")]
    media_playlists = [item for item in nested if item.split("?", 1)[0].endswith(".m3u8")]
    if media_segments:
        score += 40
        reasons.append("media segments")
    elif media_playlists:
        score += 20
        reasons.append("master playlist")
        nested_status, _nested_ct, nested_body = await fetch_small_head(media_playlists[0], headers, timeout=float(config.get("probe_timeout_seconds", 10)))
        nested_text = nested_body.decode("utf-8", errors="replace")
        nested_segments = [item for item in parse_hls_urls(nested_text, media_playlists[0]) if not item.split("?", 1)[0].endswith(".m3u8")]
        if nested_status < 400 and nested_segments:
            score += 35
            reasons.append("variant segments")
            media_segments = nested_segments
        elif nested_status >= 400:
            score -= 30
            reasons.append(f"variant http {nested_status}")
    else:
        score -= 40
        reasons.append("no media urls")
    if "#EXT-X-ENDLIST" in text and len(media_segments) < 2:
        score -= 20
        reasons.append("ended or tiny vod")
    if media_segments and deep:
        seg_status, seg_ct, seg_body = await fetch_small_head(media_segments[-1], headers, timeout=float(config.get("probe_timeout_seconds", 10)))
        if seg_status < 400 and seg_body and not looks_like_html(seg_body):
            score += 35
            reasons.append("segment readable")
        else:
            score -= 35
            reasons.append(f"segment bad:{seg_status}:{seg_ct}")
    elif media_segments:
        score += 15
        reasons.append("segment probe deferred")
    return {"ok": score >= 70, "score": score, "reasons": reasons, "resolved_url": url}


def private_iptv_source_id(prefix, entry, index):
    """Build a stable, slugified source id for an accepted private-IPTV entry (prefix + label slug)."""
    label = entry.get("title") or (entry.get("attrs") or {}).get("tvg-name") or f"Candidate {index + 1}"
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", label).strip("-").lower()[:52]
    return f"{prefix}-{slug or index + 1}"


def merge_private_iptv_sources(config, accepted, context=None):
    """Replace the auto-prefixed private sources in config with the accepted candidates (blacklisted ones dropped), keeping manual sources; returns the new source ids.

    Each accepted source is stamped with the card it was discovered for, which
    is what lets a later start refuse to ingest another event's channels.
    """
    stream = config.setdefault("stream", {})
    auto_cfg = config.get("private_iptv", {})
    prefix = auto_cfg.get("auto_source_prefix") or "private-iptv"
    existing = normalize_sources(stream.get("sources", []), stream.get("links", []))
    locked_id = str(stream.get("locked_source_id") or "")
    bl_index = blacklist_index(config.get("source_blacklist"))
    manual = [
        source
        for source in existing
        if (source.get("id") == locked_id or not str(source.get("id", "")).startswith(prefix + "-"))
        and not is_blacklisted(source, bl_index)
    ]
    auto_sources = []
    headers = {k: v for k, v in private_iptv_request_headers(auto_cfg).items() if k.lower() != "cookie"}
    for index, item in enumerate(accepted):
        entry = item["entry"]
        # Final write-barrier: blocked scraped feeds never enter stream.sources.
        if is_blacklisted(entry, bl_index):
            continue
        source_id = private_iptv_source_id(prefix, entry, index)
        label = entry.get("title") or (entry.get("attrs") or {}).get("tvg-name") or f"Private IPTV {index + 1}"
        url = entry.get("url")
        source = {
            "id": source_id,
            "label": label,
            "url": url,
            "type": source_type_for_url(None, url),
            "enabled": True,
            "headers": headers,
            "notes": f"Auto-selected from private IPTV playlist; score {item.get('score')}; {', '.join(item.get('reasons') or [])}",
            "selection_score": int(item.get("score") or 0),
            "probe_score": int((item.get("probe") or {}).get("score") or 0),
        }
        if context is not None:
            source["event_id"] = context.event_id
            source["discovered_at"] = now_ms()
            matched, _hits = context.matches(private_iptv_entry_text(entry))
            source["match_confidence"] = "exact" if matched else item.get("match_confidence", "dated")
            segment = event_context_segment_for(context, private_iptv_entry_text(entry).lower())
            if segment is not None:
                source["segment_start"] = segment[0].isoformat()
                source["segment_label"] = segment[1]
        auto_sources.append(source)
    stream["sources"] = normalize_sources([*auto_sources, *manual])
    sync_links_from_sources(stream)
    return [source["id"] for source in auto_sources]


def purge_foreign_event_sources(config, event_id):
    """Drop auto-discovered sources that belong to a *different* card.

    Run at every arming. Untagged sources (manual entries, and anything
    discovered before this feature) are left alone — the operator owns those.
    Returns the number of sources removed.
    """
    stream = config.setdefault("stream", {})
    sources = normalize_sources(stream.get("sources", []), stream.get("links", []))
    kept = [source for source in sources if str(source.get("event_id") or event_id) == str(event_id)]
    removed = len(sources) - len(kept)
    if not removed:
        return 0
    stream["sources"] = normalize_sources(kept)
    locked_id = str(stream.get("locked_source_id") or "")
    if locked_id and not any(source.get("id") == locked_id for source in stream["sources"]):
        stream["locked_source_id"] = ""
    sync_links_from_sources(stream)
    return removed


def event_source_links(config, event_id, context=None, now=None):
    """Ingest links for the event's current broadcast segment.

    A provider commonly publishes separate early-prelim, prelim and main-card
    rows. Never put a future main-card row ahead of the segment that is live.
    Untagged generic rows remain valid fallbacks.
    """
    if not event_id:
        return []
    context = active_event_context() if context is None else context
    segment_label = None
    if context is not None and str(context.event_id) == str(event_id) and context.segments:
        moment = now or datetime.now(UTC)
        segment = context.current_segment(moment) or context.segments[0]
        segment_label = segment[1].lower()
    sources = [
        source
        for source in ordered_stream_sources(config)
        if str(source.get("event_id") or "") == str(event_id)
        and (
            not segment_label
            or not source.get("segment_label")
            or str(source.get("segment_label")).lower() == segment_label
        )
    ]
    sources.sort(
        key=lambda source: (
            0 if str(source.get("segment_label") or "").lower() == segment_label else 1,
            0 if source.get("match_confidence") == "exact" else 1 if source.get("match_confidence") == "dated" else 2,
            -int(source.get("probe_score") or 0),
            -int(source.get("selection_score") or 0),
        )
    )
    return normalize_links(
        [source.get("url") for source in sources]
    )


def disable_private_iptv_sources(config, keep_event_id=None):
    """Disable (don't remove) every auto-prefixed private-IPTV source in config; return True if any were changed.

    ``keep_event_id`` spares the feeds discovered for a card that is currently
    on air: one provider hiccup mid-event must not strip the link pool out from
    under a working encode, which would leave the watchdog with nothing to
    restart onto.
    """
    stream = config.setdefault("stream", {})
    auto_cfg = config.get("private_iptv", {})
    prefix = auto_cfg.get("auto_source_prefix") or "private-iptv"
    changed = False
    sources = normalize_sources(stream.get("sources", []), stream.get("links", []))
    for source in sources:
        if keep_event_id and str(source.get("event_id") or "") == str(keep_event_id):
            continue
        if str(source.get("id", "")).startswith(prefix + "-") and source.get("enabled", True):
            source["enabled"] = False
            changed = True
    stream["sources"] = normalize_sources(sources)
    sync_links_from_sources(stream)
    return changed


def source_headers_for_url(raw_url):
    """Return the configured custom headers for whichever source matches raw_url (exact URL or same-origin path prefix), else {}."""
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
    """Build the outbound header set for a proxied upstream fetch (gooz origin/referer + browser-like defaults, overlaid with per-source headers)."""
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


# ---------------------------------------------------------------------------
# Public auto-scraper — sportsurge/gooz pages → hereisman playlist URLs.
# ---------------------------------------------------------------------------
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
    """Base64-decode value (padding-tolerant) and return it only if the result is a valid http(s) URL, else None."""
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
    """Extract .m3u8 stream URLs from an icelz page's <option> values, including base64-encoded hls/playlist query params."""
    streams: list[str] = []
    seen: set[str] = set()

    def add(candidate: str | None) -> None:
        """Add a de-duplicated valid .m3u8 URL to the streams list."""
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
    """True if a HEAD (curl -I) on the .m3u8 URL returns 200/206 with an HLS-ish content type."""
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
        """Add a cleaned, de-duplicated valid stream URL to the results."""
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
    for embed_results in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(embed_results, list):
            for u in embed_results:
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
        if event_url and valid_stream_url(event_url):
            scrape_pages.append(event_url)
        scrape_pages.extend(cfg.get("stream", {}).get("scrape_urls", []))
        scrape_pages = normalize_scrape_urls(scrape_pages)
        if not scrape_pages:
            return _AUTO_SOURCES
        bl_index = blacklist_index(cfg.get("source_blacklist"))
        all_sources: list[str] = []
        seen_sources: set[str] = set()
        for page_url in scrape_pages:
            sources = await asyncio.wait_for(_scrape_page(page_url), timeout=40)
            for source in sources:
                if source in seen_sources:
                    continue
                # Persistent blacklist: never surface a blocked scraped URL.
                if is_blacklisted(source, bl_index):
                    continue
                seen_sources.add(source)
                all_sources.append(source)
        return all_sources
    except Exception as exc:
        logger.warning("auto scrape failed: %s", exc)
        return _AUTO_SOURCES


async def _auto_scrape_loop() -> None:
    """Background loop: refresh the public auto-scraped sources every interval, pausing while an operator Stop is in effect."""
    global _AUTO_SOURCES, _AUTO_SOURCES_AT, _AUTO_SOURCES_LOCK
    while True:
        try:
            # Paused while an operator Stop is in effect (master kill switch):
            # keep the last-known red tiles but stop discovering new ones.
            if operator_stopped(load_config()):
                await asyncio.sleep(_AUTO_SCRAPE_INTERVAL)
                continue
            sources = await _run_auto_scrape()
            assert _AUTO_SOURCES_LOCK is not None  # initialised in lifespan before this loop starts
            async with _AUTO_SOURCES_LOCK:
                if sources:
                    _AUTO_SOURCES = sources
                    _AUTO_SOURCES_AT = time.time()
                    logger.info("auto scrape: %d public source(s) refreshed", len(sources))
        except Exception as exc:
            logger.warning("auto scrape loop error: %s", exc)
        await asyncio.sleep(_AUTO_SCRAPE_INTERVAL)


def private_iptv_public_runtime():
    """Return a copy of the private-IPTV runtime status with the playlist URL redacted."""
    payload = json.loads(json.dumps(PRIVATE_IPTV_RUNTIME))
    if payload.get("playlist_url"):
        payload["playlist_url"] = "***"
    return payload


async def refresh_private_iptv_sources(reason="manual", force_probe=False):
    """Coalesce all scheduled/background/operator refreshes into one writer."""
    async with PRIVATE_REFRESH_LOCK:
        return await _refresh_private_iptv_sources(reason=reason, force_probe=force_probe)


async def _refresh_private_iptv_sources(reason="manual", force_probe=False):
    """Run one private-IPTV cycle: fetch the provider playlist, score/probe candidates, merge accepted sources, and (re)start or disable the encode accordingly.

    Respects the probe budget and the persisted operator Stop; returns {ok, changed, runtime}.
    """
    global STREAM_DESIRED_STATE
    config = load_config(fresh=True)
    private_cfg = config.get("private_iptv", {})
    proc = process_metrics()
    context = active_event_context()
    if context is not None:
        reset_switch_state(context.event_id)
    health_doc = stream_health(config, proc, hls_metrics(config), force=True)
    segment_transition = bool(context is not None and context.active and sources_predate_current_segment(config, context))
    quality_upgrade = bool(
        context is not None
        and context.active
        and not live_stream_is_high_grade(
            config,
            context,
            actual_links=MANAGED_LINKS if proc.get("managed") else None,
        )
    )
    budget = private_probe_budget(config, proc=proc, health_doc=health_doc, force_probe=force_probe, context=context)
    # A live encode whose sources are not tagged for the tracked card cannot be
    # showing it. Require the mismatch to repeat before acting: a single sample
    # taken mid-write of the source list would flap the stream for nothing.
    mismatch_confirmed = False
    if context is not None and context.active and budget.get("stream_uses_private_slot") and not live_stream_is_event_matched(config, context):
        SOURCE_SWITCH_STATE["mismatch_samples"] = int(SOURCE_SWITCH_STATE.get("mismatch_samples", 0)) + 1
        mismatch_confirmed = SOURCE_SWITCH_STATE["mismatch_samples"] >= int(private_cfg.get("switch_confirm_samples", 2))
    else:
        SOURCE_SWITCH_STATE["mismatch_samples"] = 0
    update_private_probe_runtime(config, budget, mode="pending")
    PRIVATE_IPTV_RUNTIME.update(
        {
            "enabled": bool(private_cfg.get("enabled")),
            "state": "running" if private_cfg.get("enabled") else "disabled",
            "last_checked_at": now_ms(),
            "message": "Private IPTV automation is disabled." if not private_cfg.get("enabled") else "Refreshing private IPTV playlist.",
            "reasons": [],
        }
    )
    if not private_cfg.get("enabled"):
        return {"ok": True, "changed": False, "runtime": private_iptv_public_runtime()}
    if private_cfg.get("paused"):
        PRIVATE_IPTV_RUNTIME.update({"state": "paused", "message": "Private IPTV automation is paused; live ffmpeg is unchanged."})
        return {"ok": True, "changed": False, "runtime": private_iptv_public_runtime()}
    if context is not None and context.is_final:
        # The last 30 minutes are a hold, not another acquisition phase. Keep
        # the feed already on air stable until the scheduler performs the final
        # stop; a background provider sweep must not restart ffmpeg post-fight.
        PRIVATE_IPTV_RUNTIME.update(
            {
                "state": "wrapping",
                "message": "Card is final; holding the current feed through the post-fight grace period.",
            }
        )
        return {"ok": True, "changed": False, "protected": True, "runtime": private_iptv_public_runtime()}

    if should_protect_live_private_stream(config, budget, force_probe=force_probe, context=context, mismatch_confirmed=mismatch_confirmed):
        prefix = private_cfg.get("auto_source_prefix") or "private-iptv"
        active_ids = [
            source.get("id")
            for source in config.get("stream", {}).get("sources", [])
            if source.get("enabled", True) and str(source.get("id", "")).startswith(prefix + "-")
        ]
        PRIVATE_IPTV_RUNTIME.update(
            {
                "state": "active",
                "active_source_ids": active_ids,
                "message": "Healthy live fight stream pinned; scheduled rescan skipped to prevent downtime.",
                "reasons": [{"policy": "live stream protection", "health": budget.get("health_decision")}],
            }
        )
        update_private_probe_runtime(config, budget, mode="live-pinned")
        return {"ok": True, "changed": False, "protected": True, "runtime": private_iptv_public_runtime()}

    provider_url = private_cfg.get("provider_url")
    playlist_url = private_cfg.get("playlist_url")
    try:
        provider_html = ""
        if valid_stream_url(provider_url):
            provider_html = await private_iptv_fetch_text(provider_url, private_cfg)
        playlist_url = extract_private_iptv_playlist_url(provider_html, provider_url, configured_url=playlist_url)
        if not valid_stream_url(playlist_url):
            raise RuntimeError("private IPTV playlist URL was not found")
        playlist_text = await private_iptv_fetch_text(playlist_url, private_cfg, referer=provider_url)
        entries = parse_m3u_entries(playlist_text)
        rejected: list[dict[str, Any]] = []
        candidates = select_private_iptv_candidates(
            entries,
            private_cfg,
            blacklist=config.get("source_blacklist"),
            context=context,
            rejected=rejected,
        )
        if context is not None and context.segments:
            moment = datetime.now(UTC)
            current_segment = context.current_segment(moment) or context.segments[0]
            relevant = []
            for item in candidates:
                titled_segment = event_context_segment_for(
                    context,
                    private_iptv_entry_text(item["entry"]).lower(),
                )
                if titled_segment is None or titled_segment[1].lower() == current_segment[1].lower():
                    relevant.append(item)
                else:
                    rejected.append(
                        {
                            "title": item["entry"].get("title"),
                            "score": item.get("score"),
                            "reason": f"{titled_segment[1]} feed is not the current {current_segment[1]} segment",
                        }
                    )
            candidates = relevant
        # During pre-roll only exact/datetime-correlated rows qualify. Once the
        # earliest ESPN segment is actually live, availability wins: a deeply
        # probed UFC-labelled row is allowed as a visibly degraded fallback.
        # Exact acquisition continues every three minutes and replaces it.
        if not candidates and context is not None and str(context.phase) == "live":
            generic = select_private_iptv_candidates(
                entries,
                private_cfg,
                blacklist=config.get("source_blacklist"),
                context=None,
            )
            current_segment = context.current_segment(datetime.now(UTC)) or (context.segments[0] if context.segments else None)
            if current_segment is not None:
                generic = [
                    item
                    for item in generic
                    if (
                        (segment := event_context_segment_for(context, private_iptv_entry_text(item["entry"]).lower())) is None
                        or segment[1].lower() == current_segment[1].lower()
                    )
                ]
            for item in generic:
                item["match_confidence"] = "generic-ufc"
                item.setdefault("reasons", []).append("generic UFC live fallback")
            candidates = generic
            if candidates:
                rejected.append(
                    {
                        "title": candidates[0]["entry"].get("title"),
                        "score": candidates[0].get("score"),
                        "reason": f"using playable UFC fallback until {context.short_name} is identified",
                    }
                )
        SOURCE_SWITCH_STATE["last_reasons"] = rejected[:8]
        accepted = []
        probe_reasons = []
        should_probe = bool(private_cfg.get("probe_candidates", True) and budget.get("probe_allowed"))
        if private_cfg.get("probe_candidates", True) and not should_probe:
            skipped = budget.get("probe_skipped_reason") or "private playback probe skipped by budget policy"
            accepted = candidates
            probe_reasons = [
                {
                    "title": item["entry"].get("title"),
                    "score": item.get("score"),
                    "reasons": [*item.get("reasons", []), skipped],
                }
                for item in candidates
            ]
            update_private_probe_runtime(config, budget, mode="metadata-only")
        elif should_probe:
            update_private_probe_runtime(config, budget, mode="deep-probe")
            # Probe only as many feeds in parallel as the IPTV provider permits.
            # Before startup this turns the usual two available connections into a
            # much faster acquisition pass; while streaming, the encoder consumes
            # one slot and the semaphore leaves only the actual spare available.
            accept_target = int(private_cfg.get("probe_accept_target", 5))
            probe_cap = int(private_cfg.get("max_probe_attempts", 8))
            candidates_to_probe = candidates[:probe_cap]
            available_connections = max(
                1,
                int(budget.get("connection_limit") or 1)
                - int(bool(budget.get("stream_uses_private_slot"))),
            )
            semaphore = asyncio.Semaphore(available_connections)

            async def probe_candidate(candidate):
                async with semaphore:
                    return await assess_playback_candidate(candidate["entry"]["url"], private_cfg, deep=True)

            # Exclude health-monitor probes for the duration of this bounded batch;
            # the per-provider semaphore controls parallelism inside the batch.
            async with PRIVATE_PROBE_LOCK:
                assessments = await asyncio.gather(
                    *(probe_candidate(candidate) for candidate in candidates_to_probe),
                    return_exceptions=True,
                )
            for candidate, result in zip(candidates_to_probe, assessments, strict=True):
                assessment = (
                    result
                    if isinstance(result, dict)
                    else {"ok": False, "score": -100, "reasons": [f"probe error:{result}"]}
                )
                assessment_reasons = assessment.get("reasons")
                if not isinstance(assessment_reasons, list):
                    assessment_reasons = [str(assessment_reasons)] if assessment_reasons else []
                candidate["probe"] = assessment
                probe_reasons.append(
                    {
                        "title": candidate["entry"].get("title"),
                        "score": candidate.get("score"),
                        "probe_score": assessment.get("score"),
                        "reasons": [*candidate.get("reasons", []), *assessment_reasons],
                    }
                )
                if assessment.get("ok") and len(accepted) < accept_target:
                    accepted.append(candidate)
        else:
            accepted = candidates
            update_private_probe_runtime(config, budget, mode="metadata-only")
            probe_reasons = [
                {"title": item["entry"].get("title"), "score": item.get("score"), "reasons": item.get("reasons", [])}
                for item in candidates
            ]

        changed = False
        if accepted:
            SOURCE_SWITCH_STATE["acquire_attempts"] = 0
            old_links = effective_stream_links(config)
            active_ids = merge_private_iptv_sources(config, accepted, context=context)
            new_links = effective_stream_links(config)
            # Compare as a SET. merge_private_iptv_sources re-sorts by selection
            # score, and equally-scored sources (the normal case — they all score
            # 198) can come back in a different order from one provider fetch to
            # the next. Comparing the ordered list made that a "change" and tore
            # down the encode to restart it on the very same URLs.
            # A pool change is not by itself a reason to interrupt viewers. What
            # matters is whether the link ffmpeg is ACTUALLY ingesting survived the
            # refresh: if it did, the encode is unaffected and the new pool can be
            # saved silently, to be picked up by the next restart that has its own
            # reason. On 2026-08-22 the old whole-pool test killed a 19 minute old
            # encode at 1.00x with 0 dropped frames because an unrelated link had
            # entered the pool -- and the restart then abandoned the working feed.
            active_url = active_encode_link(config)
            pool_changed = set(old_links) != set(new_links)
            if active_url:
                changed = active_url not in set(new_links)
                if pool_changed and not changed:
                    event(
                        "private IPTV pool changed; live link survived, saving without a restart",
                        "ok",
                    )
            else:
                # No encode running (or the wrapper is not publishing): fall back
                # to the pool test, which is the right call when nothing is live.
                changed = pool_changed
            _reconcile_operator_stopped(config)
            save_config(config)
            PRIVATE_IPTV_RUNTIME.update(
                {
                    "state": "active",
                    "last_changed_at": now_ms() if changed else PRIVATE_IPTV_RUNTIME.get("last_changed_at"),
                    "playlist_url": playlist_url,
                    "playlist_entries": len(entries),
                    "candidate_count": len(candidates),
                    "accepted_count": len(accepted),
                    "active_source_ids": active_ids,
                    "message": f"Accepted {len(accepted)} private IPTV fight source(s).",
                    "reasons": probe_reasons[:8],
                }
            )
            live_replacement = bool(
                proc.get("managed") and (mismatch_confirmed or segment_transition or quality_upgrade)
            )
            if changed or live_replacement:
                event("private IPTV sources refreshed", "ok", {"count": len(accepted), "reason": reason})
                stream_live = bool(proc.get("managed"))
                can_restart_live = bool(force_probe or budget.get("probe_allowed") or not budget.get("stream_uses_private_slot"))
                # Anti-flap: a source change is only worth a viewer-visible
                # restart if this card has switch budget left and the last switch
                # is old enough — unless the live feed is the wrong card, which
                # always wins over "don't interrupt".
                switch_ok, switch_block = source_switch_allowed(
                    config,
                    force=mismatch_confirmed or segment_transition or quality_upgrade,
                    stream_running=stream_live,
                )
                restarted = False
                if can_restart_live and switch_ok:
                    restarted = await restart_managed_with_config("private IPTV sources refreshed")
                    if restarted and stream_live:
                        record_source_switch()
                elif not can_restart_live:
                    event("private IPTV source changes saved without restart; live private stream protected", "warn")
                else:
                    event(f"private IPTV source change held back: {switch_block}", "warn")
                schedule_owns_lifecycle = bool(ScheduleSettings.from_config(config.get("schedule")).enabled)
                if not restarted and private_cfg.get("auto_start_when_active", True) and not schedule_owns_lifecycle:
                    async with PROCESS_LOCK:
                        # Re-read operator intent from FRESH config inside the lock:
                        # an operator Stop can land during the long probe above, and
                        # gating on the pre-probe snapshot would silently re-arm a
                        # stream the operator just killed (defeating the kill switch).
                        fresh = load_config(fresh=True)
                        if (
                            not operator_stopped(fresh)
                            and STREAM_DESIRED_STATE != "stopped"
                            and (not PROCESS or PROCESS.poll() is not None)
                            and effective_stream_links(fresh)
                        ):
                            start_managed_process(fresh, None, kill_existing=True)
                            STREAM_DESIRED_STATE = "running"
            return {"ok": True, "changed": changed, "runtime": private_iptv_public_runtime()}

        # Nothing verified this cycle. Count it against the card so the public
        # backup tiles can be brought in as a fallback, and keep any feed that is
        # already carrying this card rather than emptying the pool on a blip.
        keep_event_id = context.event_id if context is not None and context.active else None
        if context is not None and context.active:
            SOURCE_SWITCH_STATE["acquire_attempts"] = int(SOURCE_SWITCH_STATE.get("acquire_attempts", 0)) + 1
        disabled = disable_private_iptv_sources(config, keep_event_id=keep_event_id)
        if disabled:
            _reconcile_operator_stopped(config)
            save_config(config)
        PRIVATE_IPTV_RUNTIME.update(
            {
                "state": "inactive",
                "last_changed_at": now_ms() if disabled else PRIVATE_IPTV_RUNTIME.get("last_changed_at"),
                "playlist_url": playlist_url,
                "playlist_entries": len(entries),
                "candidate_count": len(candidates),
                "accepted_count": 0,
                "active_source_ids": [],
                "message": "No validated fight-day private IPTV sources were accepted.",
                "reasons": probe_reasons[:8],
            }
        )
        event("private IPTV inactive", "warn", {"candidates": len(candidates), "reason": reason})
        keep_live = bool(private_cfg.get("keep_stream_live_when_inactive", True))
        schedule_owns_lifecycle = bool(ScheduleSettings.from_config(config.get("schedule")).enabled)
        if private_cfg.get("disable_stream_when_inactive", True) and not keep_live and not schedule_owns_lifecycle:
            async with PROCESS_LOCK:
                STREAM_DESIRED_STATE = "stopped"
                await stop_managed_process("private IPTV inactive; managed ffmpeg stream disabled")
        return {"ok": True, "changed": disabled, "runtime": private_iptv_public_runtime()}
    except Exception as exc:
        PRIVATE_IPTV_RUNTIME.update(
            {
                "state": "error",
                "message": f"Private IPTV refresh failed: {exc}",
                "reasons": [{"error": str(exc)}],
            }
        )
        ERRORS.append({"ts": now_ms(), "level": "error", "line": f"private IPTV refresh failed: {exc}"})
        event("private IPTV refresh failed", "bad")
        return {"ok": False, "error": str(exc), "runtime": private_iptv_public_runtime()}


async def private_iptv_loop():
    """Background loop: run the private-IPTV refresh every refresh_interval_seconds when enabled and no operator Stop is active."""
    while True:
        try:
            cfg = load_config()
            private_cfg = cfg.get("private_iptv", {})
            interval = int(private_cfg.get("refresh_interval_seconds", 900))
            PRIVATE_IPTV_RUNTIME["next_check_at"] = now_ms() + interval * 1000
            # Paused while an operator Stop is in effect (master kill switch).
            if private_cfg.get("enabled") and not private_cfg.get("paused") and not operator_stopped(cfg):
                await refresh_private_iptv_sources(reason="scheduled")
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("private IPTV loop error: %s", exc)
            await asyncio.sleep(60)


_GOOZ_ORIGIN = "https://gooz.aapmains.net"
_GOOZ_REFERER = "https://gooz.aapmains.net/"


# ---------------------------------------------------------------------------
# HLS proxy — shared upstream fetch/cache and m3u8 URL rewriting for viewers.
# ---------------------------------------------------------------------------
#: Ceiling on a single proxied body. HLS segments are seconds of video - a few
#: MB at most - so anything past this is not a segment, and buffering it would
#: put the process that supervises the encode at risk of an OOM kill.
MAX_PROXY_BODY = 24 * 1024 * 1024
#: Fail-closed key used only when no dashboard session token is configured.
#: Random per process, so signatures minted by one boot never verify against
#: another - a deployment with no secret proxies nothing rather than everything.
_PROXY_FALLBACK_KEY = secrets.token_bytes(32)

#: Lifetime of a signed proxy URL. Long enough to outlast a live playlist and any
#: reasonable player buffer, short enough that a link scraped out of somebody's
#: devtools stops working the same evening.
_PROXY_SIG_TTL = 6 * 3600


def _proxy_signing_key() -> bytes:
    """Key for proxy URL signatures, derived from the dashboard session token.

    Deliberately derived rather than a new config value: the token already exists,
    is already secret, and rotating it should invalidate outstanding proxy links
    too. Falls back to a per-process random key so a deployment without a token
    fails closed (nothing verifies) instead of open (everything verifies).
    """
    token = str((load_config().get("dashboard") or {}).get("session_token") or "")
    if not token:
        return _PROXY_FALLBACK_KEY
    return hashlib.sha256(f"obbystreams-proxy-v1:{token}".encode()).digest()


def _proxy_signature(raw_url: str, expires: int) -> str:
    return hmac.new(
        _proxy_signing_key(), f"{expires}:{raw_url}".encode(), hashlib.sha256
    ).hexdigest()[:32]


def _proxy_signature_valid(raw_url: str, expires: str, signature: str) -> bool:
    """Whether this exact URL was minted by us and has not expired."""
    try:
        deadline = int(expires)
    except (TypeError, ValueError):
        return False
    if deadline < time.time():
        return False
    return hmac.compare_digest(_proxy_signature(raw_url, deadline), signature or "")


def _proxy_url(raw_url: str) -> str:
    """Return the local /api/proxy-hls URL that fetches raw_url through this proxy.

    The URL is signed. Every address this proxy is legitimately asked for is one
    it emitted itself - entry points come from the configured source list and
    everything below them is rewritten by _rewrite_m3u8 - so a signature can be
    required without breaking playback, and it is the only thing that stops the
    endpoint being an open web proxy for the whole internet.
    """
    from urllib.parse import quote

    expires = int(time.time()) + _PROXY_SIG_TTL
    signature = _proxy_signature(raw_url, expires)
    return f"/api/proxy-hls?url={quote(raw_url, safe='')}&exp={expires}&sig={signature}"


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
        # Follow redirects manually so every hop is SSRF-validated. A public
        # URL that 302s to http://127.0.0.1 must not be followed blindly.
        current_url = raw_url
        current_headers = headers
        response = None
        for _ in range(5):
            response = await client.get(current_url, headers=current_headers, follow_redirects=False)
            if not response.is_redirect:
                break
            location = response.headers.get("location")
            if not location:
                break
            next_url = urljoin(str(response.url), location)
            if not await url_is_safe_public_async(next_url):
                raise RuntimeError(f"blocked redirect to non-public target {next_url}")
            current_url = next_url
            current_headers = proxy_request_headers(next_url)
        assert response is not None  # the loop always runs at least once
        response.raise_for_status()
        body = response.content
        if len(body) > MAX_PROXY_BODY:
            # .content materialises the whole body in memory, and the cache below
            # is capped in ENTRIES (5000) not bytes - so a handful of large
            # targets could hold gigabytes resident and OOM the process that also
            # supervises the encode. Refuse rather than buffer.
            raise RuntimeError(
                f"upstream body {len(body)} exceeds the {MAX_PROXY_BODY} byte proxy limit"
            )
        return body, response.headers.get("content-type", "")
    except (httpx.HTTPError, httpx.DecodingError) as exc:
        logger.debug("httpx proxy fetch failed for %s, trying curl fallback: %s", raw_url, exc)
        return await _proxy_upstream_fetch_curl(raw_url, headers)
    finally:
        if close_client:
            await client.aclose()


async def _proxy_upstream_fetch_curl(raw_url: str, headers: dict[str, str]) -> tuple[bytes, str]:
    """Fallback upstream fetch via a curl subprocess (no redirect following) for origins httpx can't decode; returns (body, content_type)."""
    curl_args = [
        "curl",
        "-sS",
        "--compressed",
        "--max-time",
        "12",
        "--connect-timeout",
        "4",
        # No redirect following: the initial URL is SSRF-validated upstream, and
        # curl cannot re-validate a Location hop the way the httpx path does.
        "--max-redirs",
        "0",
        # Same ceiling the httpx path enforces: communicate() buffers all stdout,
        # so without this the fallback is the OOM vector the primary path is not.
        "--max-filesize",
        str(MAX_PROXY_BODY),
        "--proto",
        "=http,https",
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
    """Split curl's `-D -` output into (body, content_type).

    Walks header blocks forward from the start, consuming 1xx/3xx preambles
    (100-continue, or a redirect chain if one is ever enabled) and stopping at
    the first final response.

    It used to take the LAST b"\r\n\r\n" in the whole output. That scans the
    body as well, so any such sequence occurring naturally inside a binary
    MPEG-TS payload became the split point and silently truncated the segment -
    which was then cached and served to every viewer for the next 120 seconds.
    """
    offset = 0
    header_end, separator_len = -1, 4
    while offset < len(raw_out):
        end, sep = raw_out.find(b"\r\n\r\n", offset), 4
        if end == -1:
            end, sep = raw_out.find(b"\n\n", offset), 2
        if end == -1:
            break
        header_end, separator_len = end, sep
        block = raw_out[offset:end]
        status = 0
        if block.startswith(b"HTTP/"):
            parts = block.split(b"\r\n", 1)[0].split(b"\n", 1)[0].split()
            if len(parts) >= 2 and parts[1].isdigit():
                status = int(parts[1])
        # A 1xx or 3xx block is a preamble; anything else is the real response.
        if not (100 <= status < 200 or 300 <= status < 400):
            break
        offset = end + sep
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
        """Resolve a playlist line/URI against the playlist URL and rewrite it to route through this proxy."""
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
                """Rewrite a matched URI="..." attribute value through proxify."""
                return f'URI="{proxify(m.group(1))}"'
            line = _M3U8_URI_RE.sub(_sub_uri, line)
            out_lines.append(line)
            continue
        # Non-comment lines are segment/key paths or URLs
        if line and not line.startswith("#"):
            line = proxify(line)
        out_lines.append(line)
    return "\n".join(out_lines) + "\n"


def _redact_url(raw_url: str) -> str:
    """scheme://host/<last path element> — never the query string.

    Provider URLs carry signed tokens in the path and query, and those are bearer
    credentials. They were being written to the journal in full by the access log
    and by this module's own warnings.
    """
    try:
        parsed = urlparse(raw_url)
    except ValueError:
        return "<unparseable>"
    tail = (parsed.path or "/").rsplit("/", 1)[-1] or "/"
    return f"{parsed.scheme}://{parsed.netloc}/…/{tail}"


def _is_configured_proxy_target(raw_url: str) -> bool:
    """Whether this exact URL is one an operator configured as a source.

    Exact match, not host match: a host allow-list would still let anyone fetch
    arbitrary paths on a provider we happen to use, spending our credentials on
    their errand via source_headers_for_url.
    """
    config = load_config()
    known: set[str] = set()
    for entry in config.get("public_sources") or []:
        if isinstance(entry, dict) and entry.get("url"):
            known.add(str(entry["url"]))
    for entry in (config.get("stream") or {}).get("sources") or []:
        if isinstance(entry, dict) and entry.get("url"):
            known.add(str(entry["url"]))
        elif isinstance(entry, str):
            known.add(entry)
    return raw_url in known


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

    # Only fetch what we ourselves published. Without this the endpoint is an
    # open web proxy: it is unauthenticated and publicly routed by nginx, and it
    # was being used as one - 115 requests over seven days from addresses that
    # have nothing to do with this service, including a search crawler.
    #
    # Two things count as ours: a URL we signed (every address inside a playlist
    # is rewritten by _rewrite_m3u8, so all derived traffic is signed), and a
    # configured source, which is how a viewer enters a stream in the first
    # place. Everything else is somebody else's errand.
    if not (
        _proxy_signature_valid(
            raw_url,
            request.query_params.get("exp", ""),
            request.query_params.get("sig", ""),
        )
        or _is_configured_proxy_target(raw_url)
    ):
        logger.warning("proxy_hls refused an unsigned target: %s", _redact_url(raw_url))
        return Response("forbidden", status_code=403, headers=cors_headers)

    if not await url_is_safe_public_async(raw_url):
        logger.warning("proxy_hls blocked non-public target %s", _redact_url(raw_url))
        return Response("forbidden host", status_code=403, headers=cors_headers)

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
    """GET /api/source-hls/{source_id}: only 'server-1' is public — it maps to the managed ufc.m3u8 via hls_proxy; all others 404."""
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
        """Minimal request shim carrying the fixed ufc.m3u8 path param into hls_proxy."""

        path_params: ClassVar[dict[str, str]] = {"path": "ufc.m3u8"}

    return await hls_proxy(_ServerOneRequest())


async def public_configured_sources(request):
    """GET /api/public-configured-sources (public, CORS): return the managed 'server-1' source with viewer counts."""
    cors = public_cors_headers()
    if request.method == "OPTIONS":
        return Response("", headers=cors)
    config = load_config()
    proc = process_metrics()
    return JSONResponse({"ok": True, "sources": public_managed_sources(config, proc), "viewers": viewer_counts_snapshot()}, headers=cors)


async def public_streams(request):
    """GET /api/public-streams (public, CORS): return the enabled public source inventory as proxied playback entries."""
    cors = public_cors_headers()
    if request.method == "OPTIONS":
        return Response("", headers=cors)
    config = load_config()
    sources = [proxied_public_source(source) for source in public_stream_inventory(config) if source.get("enabled", True)]
    return JSONResponse({"ok": True, "sources": sources, "count": len(sources)}, headers=cors)


async def public_news(request):
    """GET /api/news (public, CORS): return the visible watcher news entries."""
    cors = public_cors_headers()
    if request.method == "OPTIONS":
        return Response("", headers=cors)
    config = load_config()
    entries = public_news_entries(config)
    return JSONResponse({"ok": True, "entries": entries, "count": len(entries), "updated_at": now_ms()}, headers=cors)


async def upsert_news(request):
    """POST /api/news (guarded): create or update a watcher news entry (merging onto an existing one by id) and persist it."""
    config = load_config()
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    now_value = now_ms()
    entries = normalize_news_entries(config.get("watcher_news", []))
    entry_id = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(body.get("id") or "").strip()).strip("-").lower()
    existing_index = next((index for index, entry in enumerate(entries) if entry.get("id") == entry_id), None) if entry_id else None
    existing = entries[existing_index] if existing_index is not None else {}
    candidate = {
        **existing,
        "id": entry_id or f"news-{now_value}",
        "title": str(body.get("title", existing.get("title", ""))).strip(),
        "body": str(body.get("body", existing.get("body", ""))).strip(),
        "tone": str(body.get("tone", existing.get("tone", "info"))).strip(),
        "visible": bool(body.get("visible", existing.get("visible", True))),
        "pinned": bool(body.get("pinned", existing.get("pinned", False))),
        "created_at": existing.get("created_at") or now_value,
        "updated_at": now_value,
        "link_url": str(body.get("link_url", existing.get("link_url", ""))).strip(),
        "link_label": str(body.get("link_label", existing.get("link_label", "Open"))).strip(),
    }
    normalized = normalize_news_entries([candidate])
    if not normalized:
        return JSONResponse({"ok": False, "error": "title or body required"}, status_code=400)
    next_entry = normalized[0]
    if existing_index is None:
        entries.insert(0, next_entry)
        action = "added"
    else:
        entries[existing_index] = next_entry
        action = "updated"
    config["watcher_news"] = normalize_news_entries(entries)
    save_config(config)
    event(f"watcher news {action}", "ok", {"id": next_entry.get("id")})
    return JSONResponse({"ok": True, "entry": next_entry, "entries": public_news_entries(config, include_hidden=True)})


async def remove_news(request):
    """POST /api/news/remove (guarded): delete a watcher news entry by id and persist."""
    config = load_config()
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    entry_id = str(body.get("id") or "").strip()
    if not entry_id:
        return JSONResponse({"ok": False, "error": "id required"}, status_code=400)
    entries = normalize_news_entries(config.get("watcher_news", []))
    config["watcher_news"] = [entry for entry in entries if entry.get("id") != entry_id]
    save_config(config)
    event("watcher news removed", "warn", {"id": entry_id})
    return JSONResponse({"ok": True, "entries": public_news_entries(config, include_hidden=True)})


async def add_public_stream(request):
    """POST /api/public-streams (guarded): add a viewer-facing public source (rejecting blacklisted URLs) and persist."""
    config = load_config()
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    url = str(body.get("url", "")).strip()
    if not valid_stream_url(url):
        return JSONResponse({"ok": False, "error": "url must be http(s)"}, status_code=400)
    if is_blacklisted(url, config.get("source_blacklist")):
        return JSONResponse({"ok": False, "error": "url is blacklisted; unblock it first"}, status_code=409)
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
    """POST /api/public-streams/remove (guarded): delete a public source by id or url and persist."""
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


# ---------------------------------------------------------------------------
# Source blacklist: persistent per-source block that survives scraper cycles.
# ---------------------------------------------------------------------------


async def list_blacklist(request):
    """Return the persisted source blacklist."""
    config = load_config()
    return JSONResponse({"ok": True, "blacklist": config.get("source_blacklist", [])})


async def add_blacklist(request):
    """Block a source by url/id/label/channel.

    Persists the entry and immediately strips any now-matching source from
    ``public_sources`` and ``stream.sources`` so it disappears from every list at
    once. The scraper funnels then keep it from ever coming back.
    """
    config = load_config()
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    url = str(body.get("url") or "").strip()
    source_id = str(body.get("id") or "").strip()
    label = str(body.get("label") or "").strip()
    channel = str(body.get("channel") or "").strip()
    if not (url or source_id or label or channel):
        return JSONResponse({"ok": False, "error": "one of url/id/label/channel is required"}, status_code=400)
    entry = {
        "url": url,
        "id": source_id,
        "label": label,
        "channel": channel,
        "reason": str(body.get("reason") or "").strip(),
        "added_at": now_ms(),
    }
    blacklist = normalize_blacklist([*config.get("source_blacklist", []), entry])
    config["source_blacklist"] = blacklist
    bl_index = blacklist_index(blacklist)
    # Immediate removal from both live lists.
    config["public_sources"] = [s for s in normalize_public_sources(config.get("public_sources", [])) if not is_blacklisted(s, bl_index)]
    stream = config.setdefault("stream", {})
    stream["sources"] = [s for s in normalize_sources(stream.get("sources", []), stream.get("links", [])) if not is_blacklisted(s, bl_index)]
    sync_links_from_sources(stream)
    save_config(config)
    event("source blacklisted", "warn", {"url": url, "id": source_id, "label": label, "channel": channel})
    return JSONResponse({"ok": True, "blacklist": config["source_blacklist"]})


async def remove_blacklist(request):
    """Unblock a previously blacklisted source by url or id."""
    config = load_config()
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    url = str(body.get("url") or "").strip().lower()
    source_id = str(body.get("id") or "").strip().lower()
    if not (url or source_id):
        return JSONResponse({"ok": False, "error": "url or id is required"}, status_code=400)
    remaining = [
        entry
        for entry in normalize_blacklist(config.get("source_blacklist", []))
        if not ((url and str(entry.get("url", "")).strip().lower() == url) or (source_id and str(entry.get("id", "")).strip().lower() == source_id))
    ]
    config["source_blacklist"] = remaining
    save_config(config)
    event("source un-blacklisted", "ok", {"url": url, "id": source_id})
    return JSONResponse({"ok": True, "blacklist": config["source_blacklist"]})


_LIVE_SNAPSHOT: dict[str, Any] = {"at": 0.0, "encoded": None}
_LIVE_SNAPSHOT_LOCK = asyncio.Lock()
_LIVE_SNAPSHOT_TTL = 2.5


def _build_live_payload():
    """Build the JSON-encoded public 'live' snapshot (HLS metrics, managed sources, viewers, news) shared across all SSE clients."""
    config = load_config()
    proc = process_metrics()
    payload = {
        "ok": True,
        "server_time": now_ms(),
        "hls": hls_metrics(config),
        "sources": public_managed_sources(config, proc),
        "viewers": viewer_counts_snapshot(),
        "news": public_news_entries(config),
    }
    return json.dumps(payload, separators=(",", ":"))


async def _live_payload_encoded():
    """Return the live SSE payload from a shared snapshot, rebuilding it off-thread at most once per TTL (double-checked under a lock)."""
    # Shared across ALL SSE viewers: compute the payload at most once per TTL and
    # off the event loop, so thousands of /api/live connections cost the same as
    # one instead of each rebuilding it (and formerly each /proc-scanning) per tick.
    now = time.monotonic()
    snap = _LIVE_SNAPSHOT
    if snap["encoded"] is not None and (now - snap["at"]) < _LIVE_SNAPSHOT_TTL:
        return snap["encoded"]
    async with _LIVE_SNAPSHOT_LOCK:
        now = time.monotonic()
        if snap["encoded"] is not None and (now - snap["at"]) < _LIVE_SNAPSHOT_TTL:
            return snap["encoded"]
        encoded = await asyncio.to_thread(_build_live_payload)
        snap["encoded"] = encoded
        snap["at"] = now
        return encoded


async def live_public_events(request):
    """GET /api/live (public, CORS): Server-Sent Events stream that pushes the live snapshot every ~3s when it changes."""
    cors = public_cors_headers()
    if request.method == "OPTIONS":
        return Response("", headers=cors)

    async def stream():
        """SSE generator: emit a status event whenever the shared live payload changes."""
        last_payload = None
        while True:
            encoded = await _live_payload_encoded()
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
    assert _AUTO_SOURCES_LOCK is not None  # initialised in lifespan before any request is served
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
    """POST /api/sources/activate (guarded): enable a source and move it to position #1, then hot-restart the encode onto it."""
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


async def lock_source(request):
    """Persistently pin one source first; unlock without disturbing playback."""
    config = load_config()
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    stream = config.setdefault("stream", {})
    source_id = str(body.get("id") or "").strip()
    locked = bool(body.get("locked", True))
    sources = normalize_sources(stream.get("sources", []), stream.get("links", []))
    match = next((source for source in sources if source.get("id") == source_id), None)
    if locked and not match:
        return JSONResponse({"ok": False, "error": "source not found"}, status_code=404)
    previous_first = effective_stream_links(config)[:1]
    stream["locked_source_id"] = source_id if locked else ""
    if match:
        match["enabled"] = True
        stream["sources"] = normalize_sources([match, *[source for source in sources if source.get("id") != source_id]])
        sync_links_from_sources(stream)
    save_config(config)
    new_first = effective_stream_links(config)[:1]
    restarted = False
    if locked and previous_first != new_first:
        restarted = await restart_managed_with_config("operator locked a different source")
    event("source locked" if locked else "source unlocked", "ok", {"id": source_id})
    return JSONResponse({"ok": True, "locked_source_id": stream["locked_source_id"], "restarted": restarted, "sources": source_statuses(config, process_metrics())})


async def recover_soursignal_source(request):
    """POST /api/sources/recover-soursignal (guarded): re-scrape a soursignal source's page for a fresh HLS link, swap it in, and hot-restart."""
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


async def probe_configured_source(source, config=None, budget=None):
    """Probe one configured source's reachability/playability and store the result (green/yellow/red) in SOURCE_HEALTH."""
    state = "unknown"
    message = ""
    url = source.get("url")
    config = config or load_config()
    private_cfg = config.get("private_iptv", {})
    try:
        if not valid_stream_url(url):
            state, message = "red", "Invalid URL"
        elif is_private_soursignal_source(source, private_cfg) and budget and not budget.get("probe_allowed"):
            state, message = "unknown", budget.get("probe_skipped_reason") or "Private source probe paused"
        elif source.get("type") in {"soursignal", "page"}:
            text = await _scrape_fetch(url)
            state = "green" if text else "yellow"
            message = "Source page reachable" if text else "Source page did not respond"
        else:
            deep = not is_private_soursignal_source(source, private_cfg)
            async with PRIVATE_PROBE_LOCK:
                assessment = await assess_playback_candidate(url, normalize_private_iptv({}), headers=source.get("headers") or proxy_request_headers(url), deep=deep)
            if assessment.get("ok"):
                state = "green"
            elif assessment.get("score", 0) >= 20:
                state = "yellow"
            else:
                state = "red"
            reasons = assessment.get("reasons") or []
            message = "Playback probe passed" if state == "green" else "Playback probe weak: " + ", ".join(reasons[:3])
    except Exception as exc:
        state, message = "red", str(exc)
    SOURCE_HEALTH[source.get("id") or source.get("url")] = {
        "state": state,
        "message": message,
        "checked_at": now_ms(),
    }


async def source_health_loop():
    """Background loop: every SOURCE_HEALTH_INTERVAL, probe each enabled configured source and refresh the probe budget."""
    while True:
        try:
            config = load_config()
            proc = process_metrics()
            health_doc = stream_health(config, proc, hls_metrics(config))
            budget = private_probe_budget(config, proc=proc, health_doc=health_doc)
            update_private_probe_runtime(config, budget)
            for source in config.get("stream", {}).get("sources", []):
                if source.get("enabled", True):
                    await probe_configured_source(source, config=config, budget=budget)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("source health loop error: %s", exc)
        await asyncio.sleep(SOURCE_HEALTH_INTERVAL)


# ---------------------------------------------------------------------------
# Process supervisor — build/start/stop/restart the managed encode & watchdog.
# ---------------------------------------------------------------------------
async def read_process_output(proc):
    """Background reader: stream the managed process's stdout into LOGS/ERRORS/EVENTS, classifying each line by level."""
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
    """Build the managed encode's argv from config (encoder, output dirs, bitrates, tuning flags, source manifest, and --links)."""
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
        # Slow-upstream auto-rotation. Health scoring cannot see a feed that is
        # still emitting frames but delivering below realtime, so the supervisor
        # watches the encode rate itself and rotates links.
        "min_encode_rate": "--min-encode-rate",
        "max_drift_seconds": "--max-drift-seconds",
        "slow_source_cooldown": "--slow-source-cooldown",
        "slow_source_backoff": "--slow-source-backoff",
        "max_auto_rotations": "--max-auto-rotations",
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
    """SIGTERM the process group (escalating to SIGKILL after timeout); return True if a live process was signaled."""
    if not proc or proc.poll() is not None:
        return False
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # SIGKILL cannot land on a process wedged in uninterruptible sleep -
            # a stalled write to the output directory, or a wedged encoder ioctl.
            # This used to raise out through stop_managed_process and the Stop
            # endpoint, which had ALREADY persisted operator_stopped=True: the
            # operator got a 500, the encode kept running, and the watchdog was
            # now disarmed by the very flag Stop had just written, so nothing
            # ever reaped it. Report the failure instead of exploding on it.
            logger.error(
                "ffmpeg pid %s survived SIGKILL after %ss; leaving it orphaned",
                proc.pid,
                timeout,
            )
            return False
    return True


async def stop_managed_process(reason, kill_orphans=True):
    """Terminate the managed encode (and its reader task), reset health/state, optionally kill orphan encodes; return whether anything was stopped."""
    global MANAGED_LINKS, PROCESS, READER_TASK, STARTED_AT
    proc = PROCESS
    if not proc or proc.poll() is not None:
        if proc and proc.poll() is not None:
            RUNTIME["last_exit_code"] = proc.poll()
        PROCESS = None
        STARTED_AT = None
        MANAGED_LINKS = ()
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
    MANAGED_LINKS = ()
    STREAM_HEALTH_SCORER.reset()
    killed = await asyncio.to_thread(kill_existing_streams) if kill_orphans else []
    if killed:
        event("killed leftover stream instance(s)", "warn", {"processes": killed})
    event(reason, "warn")
    return bool(stopped or killed)


def start_managed_process(config, links, kill_existing=True):
    """Spawn the managed encode in its own session, wire up the stdout reader and health scorer; return (pid, cmd). Never touches operator-stop/desired-state.

    Raises ValueError if no links, OSError if the process can't be launched.
    """
    global MANAGED_LINKS, PROCESS, READER_TASK, STARTED_AT
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
    MANAGED_LINKS = tuple(links)
    # NOTE: intentionally does NOT touch STREAM_DESIRED_STATE / operator_stopped.
    # Desired state is owned solely by the explicit start/stop/restart endpoints,
    # so watchdog- and scraper-initiated starts can never silently re-arm a stream
    # the operator has Stopped.
    STREAM_HEALTH_SCORER.reset(pid=PROCESS.pid, started_at=STARTED_AT)
    READER_TASK = asyncio.create_task(read_process_output(PROCESS))
    RUNTIME["stream_starts"] += 1
    STREAM_START_TIMES.append(time.monotonic())
    event("stream started", "ok", {"cmd": cmd, "pid": PROCESS.pid})
    return PROCESS.pid, cmd


async def maybe_alert_stream_instability():
    """Alert Discord when the encode is restarting far more than a card requires.

    Rate-based with a cooldown, deliberately: one restart per card-segment
    transition is normal and correct, so alerting per restart would train the
    operator to ignore it. What was previously invisible is the pathological
    case — repeated watchdog restarts while viewers watch it break.
    """
    global _LAST_INSTABILITY_ALERT_AT
    if SCHEDULER is None or not SCHEDULER.notifier.active:
        return
    now_mono = time.monotonic()
    recent = [t for t in STREAM_START_TIMES if now_mono - t <= STREAM_INSTABILITY_WINDOW_SECONDS]
    if len(recent) < STREAM_INSTABILITY_THRESHOLD:
        return
    if _LAST_INSTABILITY_ALERT_AT is not None and now_mono - _LAST_INSTABILITY_ALERT_AT < STREAM_INSTABILITY_COOLDOWN_SECONDS:
        return
    _LAST_INSTABILITY_ALERT_AT = now_mono
    minutes = int(STREAM_INSTABILITY_WINDOW_SECONDS // 60)
    event(f"stream instability: {len(recent)} restarts in {minutes}m", "warn")
    await SCHEDULER.notifier.send_embed({
        "title": "⚠️ Stream is restarting repeatedly",
        "description": (
            f"The managed encode has started **{len(recent)} times in the last {minutes} minutes**. "
            "Every restart clears the segment window and interrupts all viewers, so this is visible "
            "on air. Check the upstream source and the cockpit event log."
        ),
        "color": 0xE67E22,
        "fields": [
            {"name": "Restarts (total)", "value": str(RUNTIME.get("stream_restarts", 0)), "inline": True},
            {"name": "Watchdog restarts", "value": str(RUNTIME.get("watchdog_restarts", 0)), "inline": True},
        ],
    })


MIN_REALTIME_ENCODE_RATE = 0.92
ENCODE_RATE_SUSTAIN_SECONDS = 120.0
ENCODE_RATE_ALERT_COOLDOWN_SECONDS = 1800.0
_LOW_ENCODE_RATE_SINCE: float | None = None
_LAST_LOW_RATE_ALERT_AT: float | None = None


async def maybe_alert_slow_upstream(hls):
    """Alert when the encode cannot keep up with realtime for a sustained period.

    Nothing in the health scoring looks at encode rate — it only asks whether
    ffmpeg is emitting frames and whether the playlist is fresh, both of which
    stay true while a degrading upstream delivers at 0.7x. The stream then slides
    steadily further behind live (observed: 0.73x, 92s behind and growing) while
    the cockpit reports a healthy score of 380.

    Deliberately an alert and not an automatic restart: a restart does not make a
    slow source faster, and the watchdog would relaunch onto the same primary
    link. Rotating links is the supervisor's job; a human switching sources is
    the correct response, so this exists to tell them.
    """
    global _LOW_ENCODE_RATE_SINCE, _LAST_LOW_RATE_ALERT_AT
    rate = hls.get("encode_rate")
    now_mono = time.monotonic()
    if rate is None or rate >= MIN_REALTIME_ENCODE_RATE:
        _LOW_ENCODE_RATE_SINCE = None
        return
    if _LOW_ENCODE_RATE_SINCE is None:
        _LOW_ENCODE_RATE_SINCE = now_mono
        return
    sustained = now_mono - _LOW_ENCODE_RATE_SINCE
    if sustained < ENCODE_RATE_SUSTAIN_SECONDS:
        return
    if _LAST_LOW_RATE_ALERT_AT is not None and now_mono - _LAST_LOW_RATE_ALERT_AT < ENCODE_RATE_ALERT_COOLDOWN_SECONDS:
        return
    _LAST_LOW_RATE_ALERT_AT = now_mono
    behind = (1.0 - rate) * sustained
    event(f"upstream below realtime: {rate:.2f}x for {int(sustained)}s (~{int(behind)}s behind)", "warn")
    if SCHEDULER is None or not SCHEDULER.notifier.active:
        return
    await SCHEDULER.notifier.send_embed({
        "title": "⚠️ Upstream feed is slower than realtime",
        "description": (
            f"The encode has been running at **{rate:.2f}x** for {int(sustained)}s, so the stream is "
            f"sliding roughly **{int(behind)}s** behind live and will keep drifting. Frames are still "
            "flowing, so the health score looks fine — switching to another source is the fix."
        ),
        "color": 0xE67E22,
        "fields": [{"name": "Encode rate", "value": f"{rate:.2f}x", "inline": True},
                   {"name": "Sustained", "value": f"{int(sustained)}s", "inline": True}],
    })


async def restart_managed_with_config(reason):
    """If the managed encode is running, stop and restart it with freshly-loaded config/links (under PROCESS_LOCK); return whether it restarted."""
    global PROCESS
    async with PROCESS_LOCK:
        if not PROCESS or PROCESS.poll() is not None:
            return False
        # Read the live link BEFORE stopping: once ffmpeg is gone the wrapper
        # stops republishing it and active_encode_link() goes stale by design.
        active_url = active_encode_link(load_config())
        await stop_managed_process(f"stream stopped for restart: {reason}")
        # Let the provider release the connection slot before asking for another.
        await asyncio.sleep(PROVIDER_DRAIN_SECONDS)
        event(f"restarting stream: {reason}", "warn")
        config = load_config()
        # Smoothest-first, then pull the still-working link to the front. Order
        # matters: a link we KNOW is currently delivering beats a historical
        # score, but everything behind it should still be sorted by quality so a
        # failure walks toward good feeds instead of down the config order.
        links = links_with_active_first(links_by_quality(effective_stream_links(config)), active_url)
        if active_url and links and links[0] == active_url:
            event("restart will resume on the link that was already working", "ok")
        try:
            start_managed_process(config, links, kill_existing=True)
            RUNTIME["stream_restarts"] += 1
        except (OSError, ValueError) as exc:
            event(f"stream restart failed: {exc}", "bad")
            ERRORS.append({"ts": now_ms(), "level": "error", "line": f"stream restart failed: {exc}"})
            return False
        return True


# ---------------------------------------------------------------------------
# UFC auto-schedule — bridge between the cockpit and the obbyschedule package.
#
# The scheduler never imports this module; it calls back in through the three
# coroutines below. All of them take PROCESS_LOCK, which is what serialises them
# against a human pressing Stop: whichever grabs the lock first wins, and the
# loser sees the other's effect (suppression, or a cleared operator Stop).
# ---------------------------------------------------------------------------
async def suppress_current_schedule_event():
    """Mark the event the scheduler is tracking as operator-vetoed.

    Called from the Stop endpoint. The veto is scoped to the *current* card, so
    the scheduler stays armed for the next one — Stop means "not this event",
    not "never again". Returns the suppressed event id, if any.
    """
    if SCHEDULER is None:
        return None
    event_id = SCHEDULER.state.current_event_id
    if not event_id:
        return None
    SCHEDULER.state.suppressed_event_id = event_id
    SCHEDULER.state.started_by_scheduler = False
    await SCHEDULER.persist()
    return event_id


async def clear_schedule_suppression():
    """Lift any per-event veto — a manual Start hands control back to the scheduler."""
    if SCHEDULER is None or SCHEDULER.state.suppressed_event_id is None:
        return
    SCHEDULER.state.suppressed_event_id = None
    await SCHEDULER.persist()


def schedule_start_links(config, context):
    """The links a scheduled start is allowed to ingest, and why.

    With a tracked card, only feeds discovered *for that card* qualify. The old
    behaviour — start on whatever links happen to be on disk — is what put the
    previous week's channels on air for the whole of 2026-08-01, because a stale
    feed that still decodes looks identical to a good one from ffmpeg's side.
    Public backup sources are pulled in once the private path has failed to
    produce anything for several cycles, so a provider outage does not mean a
    dark card.
    """
    if context is None:
        return effective_stream_links(config), "no tracked card; using the configured link pool"
    links = event_source_links(config, context.event_id)
    if links:
        return links, f"{len(links)} source(s) verified for {context.short_name}"
    attempts = int(SOURCE_SWITCH_STATE.get("acquire_attempts", 0))
    fallback_after = int(config.get("private_iptv", {}).get("public_fallback_after_attempts", 4))
    if str(context.phase) == "live" and attempts >= fallback_after:
        public = normalize_links(current_auto_sources())
        if public:
            return public, f"no private feed after {attempts} attempts; falling back to {len(public)} public source(s)"
    return [], f"no source verified for {context.short_name} yet (attempt {attempts})"


def schedule_best_source_confidence(config, context):
    """Confidence of the first current-segment source selected for a card."""
    if context is None:
        return None
    urls = event_source_links(config, context.event_id, context=context)
    by_url = {source.get("url"): source for source in ordered_stream_sources(config)}
    for url in urls:
        confidence = str((by_url.get(url) or {}).get("match_confidence") or "")
        if confidence:
            return confidence
    return SOURCE_SWITCH_STATE.get("selected_confidence")


async def schedule_start_stream(reason):
    """Arm the stream for a scheduled card. Returns True once the cockpit is armed.

    "Armed" deliberately means *the operator Stop is lifted and this card is
    owned*, not "ffmpeg is up". Holding with the encode down is a legitimate
    outcome: streaming an unidentified feed is worse than streaming nothing, and
    reporting success is what keeps the scheduler in charge so it retries on the
    acquisition cadence and still performs the stand-down at the end.
    """
    global STREAM_DESIRED_STATE
    if SCHEDULER is None:
        return False
    async with PROCESS_LOCK:
        state = SCHEDULER.state
        if state.current_event_id and state.suppressed_event_id == state.current_event_id:
            event("auto-schedule start skipped: operator stopped this event", "warn")
            return StartResult(StartStatus.FAILED, "operator stopped this event")
        config = load_config(fresh=True)
        set_operator_stopped(config, False)
        context = active_event_context()
        # Last week's sources can never survive into this card's link pool.
        removed = purge_foreign_event_sources(config, context.event_id) if context is not None else 0
        if removed:
            event(f"auto-schedule dropped {removed} source(s) from a previous card", "warn")
            save_config(config)
        links, detail = schedule_start_links(config, context)
        if not links:
            if PROCESS and PROCESS.poll() is None:
                # A decoding process is not evidence that it carries this card.
                # Take the wrong/unidentified feed off air while acquisition
                # continues, otherwise a stale 24/7 source can look perfectly
                # healthy and survive the entire event.
                await stop_managed_process("unverified running feed quarantined by auto-schedule")
                STREAM_DESIRED_STATE = "stopped"
            SOURCE_SWITCH_STATE["last_error"] = detail
            event(f"auto-schedule armed ({reason}); holding: {detail}", "warn")
            return StartResult(StartStatus.AWAITING_SOURCE, detail)
        try:
            replacing = bool(PROCESS and PROCESS.poll() is None)
            if replacing:
                await stop_managed_process("stream stopped to install a verified UFC source")
            start_managed_process(config, links, kill_existing=True)
            STREAM_DESIRED_STATE = "running"
        except (OSError, ValueError) as exc:
            event(f"auto-schedule start failed: {exc}", "bad")
            ERRORS.append({"ts": now_ms(), "level": "error", "line": f"auto-schedule start failed: {exc}"})
            return StartResult(StartStatus.FAILED, str(exc))
        verb = "replaced the running feed" if replacing else "started the stream"
        event(f"auto-schedule {verb} ({reason}); {detail}", "ok")
        confidence = schedule_best_source_confidence(config, context)
        if "public source" in detail:
            confidence = "public-generic"
        SOURCE_SWITCH_STATE["selected_confidence"] = confidence
        return StartResult(StartStatus.STARTED, detail, confidence)


async def schedule_stop_stream(reason):
    """Stand the stream down after a card, leaving it in scheduled standby."""
    global STREAM_DESIRED_STATE, WATCHDOG_LAST_ACTION
    async with PROCESS_LOCK:
        STREAM_DESIRED_STATE = "stopped"
        WATCHDOG_LAST_ACTION = time.monotonic()
        config = load_config(fresh=True)
        set_operator_stopped(config, True, StopReason.SCHEDULE.value)
        # Retire this card's feeds. Left enabled they would be the first thing a
        # future start reached for, a week after their tokens went dead.
        if disable_private_iptv_sources(config):
            config.setdefault("stream", {})["locked_source_id"] = ""
            save_config(config)
        await stop_managed_process(f"stream stopped by auto-schedule: {reason}")
        event(f"auto-schedule stood the stream down ({reason})", "ok")
        return True


class CockpitSourceResolver:
    """The scheduler's view of source discovery (``obbyschedule.SourceResolver``).

    Keeps the package free of any cockpit import: the scheduler hands over the
    tracked card, and this bridges it to the private-IPTV scraper and the public
    backup scraper.
    """

    async def refresh(self, reason, context=None):
        """Resolve sources for the tracked card before anything goes on air."""
        self.publish(context)
        # Private and public discovery are independent and bounded; doing them
        # concurrently keeps a 10-minute pre-roll from being consumed by serial
        # network/probe time.
        results = await asyncio.gather(
            refresh_private_iptv_sources(reason=reason, force_probe=False),
            refresh_public_backup_sources(),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.warning("scheduled source resolver failed: %s", result)

    def is_satisfied(self, context):
        """True only for a current-segment, deeply probed, healthy live feed."""
        if context is None:
            return False
        config = load_config()
        proc = process_metrics()
        if not proc.get("managed"):
            return False
        health = stream_health(config, proc, hls_metrics(config))
        if str(health.get("decision") or "").lower() != "healthy":
            return False
        current_urls = set(event_source_links(config, context.event_id, context=context))
        actual_urls = set(MANAGED_LINKS)
        sources = [
            source
            for source in ordered_stream_sources(config)
            if source.get("url") in current_urls and source.get("url") in actual_urls
        ]
        return any(
            int(source.get("probe_score") or 0) >= 80
            and source.get("match_confidence") in {"exact", "dated"}
            for source in sources
        )

    def is_stream_acceptable(self, context):
        """Whether ffmpeg's *actual* ingest pool may stay on air for this segment.

        This is deliberately separate from ``is_satisfied``. A deeply probed
        generic fallback is acceptable after the bell while exact acquisition
        continues, whereas a stale or future-segment process must be replaced
        even if its output health is green.
        """
        if context is None or not process_metrics().get("managed"):
            return False
        config = load_config()
        allowed = set(event_source_links(config, context.event_id, context=context))
        actual = set(MANAGED_LINKS)
        if actual & allowed:
            return True
        return bool(
            str(context.phase) == "live"
            and SOURCE_SWITCH_STATE.get("selected_confidence") == "public-generic"
            and actual.intersection(current_auto_sources())
        )

    def publish(self, context):
        """Point the scraper at a card (or clear it once the card is done)."""
        global ACTIVE_EVENT_CONTEXT
        ACTIVE_EVENT_CONTEXT = context
        if context is None:
            reset_switch_state(None)
        else:
            reset_switch_state(context.event_id)


async def refresh_public_backup_sources():
    """Re-scrape the public backup sources now instead of waiting for the loop."""
    global _AUTO_SOURCES, _AUTO_SOURCES_AT
    sources = await _run_auto_scrape()
    if not sources or _AUTO_SOURCES_LOCK is None:
        return list(_AUTO_SOURCES)
    async with _AUTO_SOURCES_LOCK:
        _AUTO_SOURCES = sources
        _AUTO_SOURCES_AT = time.time()
    event("public backup sources refreshed for the tracked card", "ok", {"count": len(sources)})
    return sources


def schedule_source_state(config=None):
    """How source acquisition is going for the tracked card.

    Answers the question the cockpit could not answer during the 2026-08-01
    card: *which* feed is on air, whether it belongs to tonight's event, and if
    nothing is on air, what was rejected and why.
    """
    context = active_event_context()
    config = config or load_config()
    matched = [
        {
            "id": source.get("id"),
            "label": source.get("label"),
            "discovered_at": source.get("discovered_at"),
            "match_confidence": source.get("match_confidence"),
            "segment_label": source.get("segment_label"),
            "selection_score": source.get("selection_score"),
            "probe_score": source.get("probe_score"),
        }
        for source in ordered_stream_sources(config)
        if context is not None and str(source.get("event_id") or "") == str(context.event_id)
    ]
    return {
        "event_id": context.event_id if context else None,
        "event_matched_sources": matched,
        "event_matched": bool(matched),
        "terms": list(context.terms) if context else [],
        "acquire_attempts": int(SOURCE_SWITCH_STATE.get("acquire_attempts", 0)),
        "switches": int(SOURCE_SWITCH_STATE.get("switches", 0)),
        "mismatch_samples": int(SOURCE_SWITCH_STATE.get("mismatch_samples", 0)),
        "rejected": list(SOURCE_SWITCH_STATE.get("last_reasons") or []),
        "last_error": SOURCE_SWITCH_STATE.get("last_error") or "",
        "selected_confidence": schedule_best_source_confidence(config, context),
        "refresh_interval_seconds": int(config.get("schedule", {}).get("acquisition_poll_seconds", 180)),
    }


def schedule_snapshot():
    """Compact scheduler state for the status payload and ``GET /api/schedule``."""
    if SCHEDULER is None:
        return {"enabled": False, "phase": "idle", "action": "idle", "reason": "scheduler not running"}
    return {**SCHEDULER.snapshot(), "source_state": schedule_source_state()}


async def get_schedule(request):
    """GET /api/schedule (guarded): the auto-schedule state, tracked card, and countdown."""
    return JSONResponse({"ok": True, "schedule": schedule_snapshot()})


async def update_schedule(request):
    """POST /api/schedule (guarded): toggle auto-schedule, or fire a test Discord embed."""
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    if "enabled" in body:
        config = load_config()
        section = dict(config.get("schedule") or {})
        section["enabled"] = bool(body.get("enabled"))
        config["schedule"] = section
        _reconcile_operator_stopped(config)
        save_config(config)
        event(f"auto-schedule {'enabled' if section['enabled'] else 'disabled'} by operator", "warn")
        if SCHEDULER is not None:
            SCHEDULER.reload_settings()

    if body.get("coming_up"):
        if SCHEDULER is None:
            return JSONResponse({"ok": False, "error": "scheduler is not running"}, status_code=503)
        if not SCHEDULER.notifier.active:
            return JSONResponse({"ok": False, "error": "no Discord webhook configured"}, status_code=400)
        upcoming = await SCHEDULER.load_upcoming_event()
        if upcoming is None:
            return JSONResponse({"ok": False, "error": "no upcoming UFC card on the ESPN calendar"}, status_code=404)
        sent = await SCHEDULER.announce_coming_up(upcoming, force=True)
        return JSONResponse({"ok": sent, "sent": sent, "event": upcoming.name, "schedule": schedule_snapshot()})

    if body.get("test_notification"):
        if SCHEDULER is None:
            return JSONResponse({"ok": False, "error": "scheduler is not running"}, status_code=503)
        await SCHEDULER.ensure_loaded()
        SCHEDULER.reload_settings()
        if not SCHEDULER.notifier.active:
            return JSONResponse({"ok": False, "error": "no Discord webhook configured"}, status_code=400)
        upcoming = SCHEDULER.next_after(SCHEDULER.state.calendar, datetime.now(UTC))
        sent = await SCHEDULER.notifier.send_embed(SCHEDULER.notifier.builder.test(upcoming.label if upcoming else None))
        return JSONResponse({"ok": sent, "sent": sent, "schedule": schedule_snapshot()})

    return JSONResponse({"ok": True, "schedule": schedule_snapshot()})


async def start_stream(request):
    """POST /api/stream/start (guarded): clear the persisted operator Stop and start the managed encode (409 if already running)."""
    global PROCESS, STREAM_DESIRED_STATE
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    config = load_config()
    # Clear the persisted operator Stop so this Start sticks and re-arms the
    # watchdog + scrapers.
    set_operator_stopped(config, False)
    # A human taking manual control lifts any per-event suppression, so the
    # scheduler is free to manage the *next* card normally.
    await clear_schedule_suppression()
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
    """Persistent operator Stop: kills the managed ffmpeg AND pauses both scrapers.

    Sets ``stream.operator_stopped`` in the config so the stop survives supervisor
    ticks and full service/host restarts — the stream stays down until an explicit
    Start/Restart.
    """
    global STREAM_DESIRED_STATE, WATCHDOG_LAST_ACTION
    async with PROCESS_LOCK:
        STREAM_DESIRED_STATE = "stopped"
        WATCHDOG_LAST_ACTION = time.monotonic()
        set_operator_stopped(load_config(), True, StopReason.MANUAL.value)
        # Pressing Stop *during* a card means "not this one" — suppress only the
        # event currently being tracked so the scheduler still arms the next one.
        suppressed = await suppress_current_schedule_event()
        # Never let a failed kill become a 500 with operator_stopped already
        # persisted: that combination disarms the watchdog while the encode is
        # still running, and nothing reaps it afterwards.
        try:
            stopped = await stop_managed_process(
                "stream stopped by operator (persisted; scrapers paused)"
            )
        except Exception:
            logger.exception("operator stop failed to terminate the encode")
            stopped = False
        orphan_pid = PROCESS.pid if PROCESS is not None and PROCESS.poll() is None else None
        if orphan_pid is not None:
            event(f"stop left ffmpeg pid {orphan_pid} running", "bad")
        return JSONResponse(
            {
                "ok": orphan_pid is None,
                "stopped": stopped,
                "orphan_pid": orphan_pid,
                "operator_stopped": True,
                "stop_reason": StopReason.MANUAL.value,
                "suppressed_event_id": suppressed,
            }
        )


async def restart_stream(request):
    """Restart the managed stream, clearing any persisted operator Stop first."""
    global STREAM_DESIRED_STATE
    async with PROCESS_LOCK:
        STREAM_DESIRED_STATE = "running"
        set_operator_stopped(load_config(), False)
        await clear_schedule_suppression()
        if PROCESS and PROCESS.poll() is None:
            await stop_managed_process("stream stopped")
    return await start_stream(request)


async def watchdog_loop():
    """Background supervisor: every ~2s, auto-restart the managed encode if it exited or the health scorer confirms failure, honoring operator Stop and a cooldown."""
    global WATCHDOG_LAST_ACTION, PROCESS, STARTED_AT
    while True:
        try:
            await asyncio.sleep(2)
            config = load_config()
            stream = config.get("stream", {})
            # Evaluated before the Stop guard: a stopped stream still deserves the
            # alert for the restart storm that preceded the operator stopping it.
            await maybe_alert_stream_instability()
            # Operator Stop is a hard idle: no auto-recovery of either the exited
            # or the stalled branch until a human Starts the stream again.
            if operator_stopped(config):
                continue
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
                        links, detail = schedule_start_links(config, active_event_context())
                        if not links:
                            event(f"watchdog skipped restart: {detail}", "warn")
                            continue
                        links = links_with_active_last(links_by_quality(links), active_encode_link(config))
                        try:
                            start_managed_process(config, links, kill_existing=True)
                        except (OSError, ValueError) as exc:
                            event(f"watchdog restart failed: {exc}", "bad")
                            ERRORS.append({"ts": now_ms(), "level": "error", "line": f"watchdog restart failed: {exc}"})
                    continue
                proc = process_metrics()
                hls = hls_metrics(config)
                # Checked before the decision gate: a slow upstream keeps the
                # score healthy by design, so this would never be reached if it
                # sat behind a "failed" verdict.
                await maybe_alert_slow_upstream(hls)
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
                failed_url = active_encode_link(config)
                await stop_managed_process(f"stream stopped for watchdog: {reason}")
                await asyncio.sleep(PROVIDER_DRAIN_SECONDS)
                # While a card is tracked the watchdog is held to the same bar as
                # a scheduled start: recover onto this event's feeds or not at all.
                links, detail = schedule_start_links(config, active_event_context())
                if not links:
                    event(f"watchdog skipped restart: {detail}", "warn")
                    continue
                links = links_with_active_last(links_by_quality(links), failed_url)
                try:
                    start_managed_process(config, links, kill_existing=True)
                except (OSError, ValueError) as exc:
                    event(f"watchdog restart failed: {exc}", "bad")
                    ERRORS.append({"ts": now_ms(), "level": "error", "line": f"watchdog restart failed: {exc}"})
        except asyncio.CancelledError:
            break
        except Exception as exc:
            event(f"watchdog loop error: {exc}", "warn")


# ---------------------------------------------------------------------------
# Local HLS/DASH file server — serve or proxy the managed output to viewers.
# ---------------------------------------------------------------------------
def hls_content_type(path):
    """Return the MIME type for an HLS/DASH path by extension (.mpd/.m3u8/.ts/.m4s/.mp4), else octet-stream."""
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
    """Sanitize a requested HLS path (default ufc.m3u8), rejecting empty or '..' traversal paths with None."""
    path = str(value or "ufc.m3u8").lstrip("/")
    if not path or ".." in Path(path).parts:
        return None
    return path


def rewrite_playlist(text):
    """Rewrite relative segment/playlist references in an m3u8 to absolute /hls/ paths (absolute URLs left unchanged)."""
    rewritten = []
    for line in text.splitlines():
        if line and not line.startswith("#") and not line.startswith(("http://", "https://")):
            rewritten.append(f"/hls/{line.lstrip('/')}")
        else:
            rewritten.append(line)
    return "\n".join(rewritten) + "\n"


def hls_upstream_urls(config, path):
    """Build the ordered list of remote fallback URLs (configured public DASH/HLS bases + fight.nswfiles.com) for a requested path."""
    stream = config.get("stream", {})
    public_dash_url = stream.get("public_dash_url", "")
    public_hls_url = stream.get("public_hls_url", "")
    candidates = []
    if public_dash_url and path.endswith((".mpd", ".m4s", ".mp4")):
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
    """GET /hls/{path}: serve the managed encode's local HLS/DASH file (rewriting playlists), falling back to remote upstreams when absent."""
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


FFMPEG_LOG_RETENTION_DAYS = 14


def prune_ffmpeg_logs(days=FFMPEG_LOG_RETENTION_DAYS):
    """Delete ffmpeg attempt logs older than ``days``; returns how many went.

    Runs once at boot in a worker thread. Blocking, so never call it on the
    event loop — the directory can hold six figures' worth of files.
    """
    stream = load_config().get("stream", {})
    log_dir = Path(stream.get("ffmpeg_log_dir") or "ffmpegLogs")
    if not log_dir.is_absolute():
        log_dir = APP_DIR / log_dir
    if not log_dir.is_dir():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for path in log_dir.glob("*.log"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("pruned %d ffmpeg log(s) older than %d days", removed, days)
    return removed


async def index(request):
    """GET /: serve the cockpit SPA index.html."""
    return FileResponse(STATIC_DIR / "index.html")


def static_asset(name, media_type=None):
    """Return a route handler that serves the named file from STATIC_DIR with an optional media type."""
    async def handler(request):
        """Serve the bound static asset file."""
        return FileResponse(STATIC_DIR / name, media_type=media_type)

    return handler


# ---------------------------------------------------------------------------
# App lifespan & routes — start/stop background tasks and wire up the ASGI app.
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app):
    """ASGI lifespan: honor a persisted operator Stop, init the shared HTTP client and Arango queue, launch all background loops, and tear them down (stopping the encode) on shutdown."""
    global WATCHDOG_TASK, ARANGO_QUEUE, ARANGO_WORKER_TASK, _AUTO_SCRAPE_TASK, _AUTO_SOURCES_LOCK, _HTTPX_CLIENT, SOURCE_HEALTH_TASK, PRIVATE_IPTV_TASK, STREAM_DESIRED_STATE, SCHEDULER, SCHEDULE_TASK
    # Auto-scheduled installations always boot into standby. The schedule's
    # first event-aware tick is the only path that may select links and start
    # ffmpeg, so the watchdog can never win startup with last week's feed.
    initial_config = load_config()
    STREAM_DESIRED_STATE = boot_stream_desired_state(initial_config)
    if operator_stopped(initial_config):
        event("boot: operator Stop is in effect; stream + scrapers idle until Start", "warn")
    elif STREAM_DESIRED_STATE == "stopped":
        event("boot: auto-schedule standby; waiting for a verified UFC event source", "ok")
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
    # Every ffmpeg attempt writes a log and nothing ever removed them: the
    # directory had grown to ~150k files / 13GB. Prune off the event loop.
    _BACKGROUND_TASKS.add(pruned := asyncio.create_task(asyncio.to_thread(prune_ffmpeg_logs)))
    pruned.add_done_callback(_BACKGROUND_TASKS.discard)
    WATCHDOG_TASK = asyncio.create_task(watchdog_loop())
    SOURCE_HEALTH_TASK = asyncio.create_task(source_health_loop())
    PRIVATE_IPTV_TASK = asyncio.create_task(private_iptv_loop())

    # UFC auto-schedule. It shares the httpx pool above and reaches back into the
    # cockpit only through the injected coroutines, so this module stays the only
    # place that knows about PROCESS_LOCK and the operator Stop switch.
    SCHEDULER = UfcScheduler(
        client=_HTTPX_CLIENT,
        load_config=lambda: load_config(),
        start_stream=schedule_start_stream,
        stop_stream=schedule_stop_stream,
        sources=CockpitSourceResolver(),
        event_log=event,
    )
    SCHEDULE_TASK = asyncio.create_task(SCHEDULER.run(lambda: bool(PROCESS and PROCESS.poll() is None)))

    async def _proxy_cache_cleanup_loop():
        """Background loop: prune the proxy cache every 60s."""
        while True:
            await asyncio.sleep(60)
            try:
                await _PROXY_CACHE.cleanup()
            except Exception as exc:
                logger.warning("proxy cache cleanup error: %s", exc)

    _PROXY_CACHE_TASK = asyncio.create_task(_proxy_cache_cleanup_loop())

    # Viewer highscores: load persisted stats and flush periodically.
    load_viewer_stats()

    async def _viewer_stats_flush_loop():
        """Background loop: persist viewer stats to disk every 30s."""
        while True:
            await asyncio.sleep(30)
            try:
                await flush_viewer_stats()
            except Exception as exc:
                logger.warning("viewer stats flush error: %s", exc)

    _VIEWER_STATS_TASK = asyncio.create_task(_viewer_stats_flush_loop())

    # Kick off first scrape immediately in the background, then loop
    async def _scrape_then_loop():
        """Run one immediate public scrape at boot (unless operator-stopped), then hand off to the periodic auto-scrape loop."""
        global _AUTO_SOURCES, _AUTO_SOURCES_AT
        # Skip the immediate kick-off scrape while an operator Stop is in effect;
        # the loop itself also re-checks each tick.
        if not operator_stopped(load_config()):
            sources = await _run_auto_scrape()
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
        if PRIVATE_IPTV_TASK:
            PRIVATE_IPTV_TASK.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await PRIVATE_IPTV_TASK
        if SCHEDULE_TASK:
            SCHEDULE_TASK.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await SCHEDULE_TASK
        if _PROXY_CACHE_TASK:
            _PROXY_CACHE_TASK.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _PROXY_CACHE_TASK
        if _VIEWER_STATS_TASK:
            _VIEWER_STATS_TASK.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _VIEWER_STATS_TASK
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
    Route("/metrics", guarded(metrics)),
    Route("/api/auth/login", login, methods=["POST"]),
    Route("/api/status", guarded(status)),
    Route("/api/sources", guarded(list_sources), methods=["GET"]),
    Route("/api/sources/activate", guarded(activate_source), methods=["POST"]),
    Route("/api/sources/lock", guarded(lock_source), methods=["POST"]),
    Route("/api/sources/recover-soursignal", guarded(recover_soursignal_source), methods=["POST"]),
    Route("/api/private-iptv", guarded(private_iptv_status), methods=["GET"]),
    Route("/api/private-iptv/refresh", guarded(private_iptv_refresh), methods=["POST"]),
    Route("/api/private-iptv/control", guarded(private_iptv_control), methods=["POST"]),
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
    Route("/api/blacklist", guarded(list_blacklist), methods=["GET"]),
    Route("/api/blacklist", guarded(add_blacklist), methods=["POST"]),
    Route("/api/blacklist/remove", guarded(remove_blacklist), methods=["POST"]),
    Route("/api/news", public_news, methods=["GET", "OPTIONS"]),
    Route("/api/news", guarded(upsert_news), methods=["POST"]),
    Route("/api/news/remove", guarded(remove_news), methods=["POST"]),
    Route("/api/public-configured-sources", public_configured_sources, methods=["GET", "OPTIONS"]),
    Route("/api/live", live_public_events, methods=["GET", "OPTIONS"]),
    Route("/api/viewers", viewer_counts, methods=["GET", "POST", "OPTIONS"]),
    Route("/api/highscores", viewer_highscores, methods=["GET", "OPTIONS"]),
    Route("/api/proxy-hls", proxy_hls, methods=["GET", "HEAD", "OPTIONS"]),
    Route("/api/source-hls/{source_id}", source_hls, methods=["GET", "HEAD", "OPTIONS"]),
    Route("/api/stream/start", guarded(start_stream), methods=["POST"]),
    Route("/api/stream/stop", guarded(stop_stream), methods=["POST"]),
    Route("/api/stream/restart", guarded(restart_stream), methods=["POST"]),
    Route("/api/schedule", guarded(get_schedule), methods=["GET"]),
    Route("/api/schedule", guarded(update_schedule), methods=["POST"]),
    Route("/api/arango", guarded(arango_status)),
    Route("/api/nvidia-smi", guarded(nvidia_smi_status)),
    Route("/hls/{path:path}", hls_proxy),
    Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
]

app = Starlette(debug=False, routes=routes, lifespan=lifespan)
