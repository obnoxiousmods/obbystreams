#!/usr/bin/env python3
import asyncio
import base64
import contextlib
import csv
import glob
import io
import json
import os
import secrets
import signal
import subprocess
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx
import psutil
import yaml
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

CONFIG_PATH = Path(os.environ.get("OBBYSTREAMS_CONFIG", "/etc/obbystreams/obbystreams.yaml"))
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
APP_STARTED_AT = None

EVENTS: deque[dict] = deque(maxlen=300)
LOGS: deque[dict] = deque(maxlen=600)
ERRORS: deque[dict] = deque(maxlen=200)
PROCESS = None
STARTED_AT = None
READER_TASK = None
PROCESS_LOCK = asyncio.Lock()
WATCHDOG_TASK = None
WATCHDOG_LAST_ACTION = 0.0
NVIDIA_SMI_CACHE_SECONDS = 5.0
NVIDIA_SMI_CACHE: dict = {"at": 0.0, "payload": None}
NVIDIA_SMI_LOCK = asyncio.Lock()
ARANGO_WORKER_TASK = None
ARANGO_QUEUE_MAX = 1200
ARANGO_QUEUE: asyncio.Queue | None = None
ARANGO_RETRY_MAX_ATTEMPTS = 3
RUNTIME = {
    "stream_starts": 0,
    "stream_restarts": 0,
    "watchdog_restarts": 0,
    "start_failures": 0,
    "last_exit_code": None,
    "arango_dropped_writes": 0,
    "arango_write_failures": 0,
}


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
        "public_hls_url": "",
        "bitrate": "6M",
        "audio_bitrate": "192k",
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
    },
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


def normalize_config(config):
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    if not isinstance(config, dict):
        return merged
    for section in ("server", "dashboard", "stream", "arangodb"):
        raw_section = config.get(section, {})
        if not isinstance(raw_section, dict):
            continue
        merged[section].update(raw_section)
    stream = merged["stream"]
    stream["links"] = normalize_links(stream.get("links", []))
    stream["output_dir"] = str(stream.get("output_dir") or DEFAULT_CONFIG["stream"]["output_dir"])
    stream["ffmpeg_log_dir"] = str(stream.get("ffmpeg_log_dir") or DEFAULT_CONFIG["stream"]["ffmpeg_log_dir"])
    stream["command"] = str(stream.get("command") or DEFAULT_CONFIG["stream"]["command"])
    stream["encoder"] = str(stream.get("encoder") or DEFAULT_CONFIG["stream"]["encoder"])
    if stream["encoder"] not in ENCODER_CHOICES:
        stream["encoder"] = DEFAULT_CONFIG["stream"]["encoder"]
    stream["bitrate"] = str(stream.get("bitrate") or DEFAULT_CONFIG["stream"]["bitrate"])
    stream["audio_bitrate"] = str(stream.get("audio_bitrate") or DEFAULT_CONFIG["stream"]["audio_bitrate"])
    stream["public_hls_url"] = str(stream.get("public_hls_url") or "")
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


def load_config():
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            return normalize_config(yaml.safe_load(f) or {})
    except FileNotFoundError:
        ERRORS.append({"ts": now_ms(), "level": "error", "line": f"config missing: {CONFIG_PATH}"})
        return normalize_config({})
    except (yaml.YAMLError, OSError) as exc:
        ERRORS.append({"ts": now_ms(), "level": "error", "line": f"config load failed: {exc}"})
        return normalize_config({})


def save_config(config):
    tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
    normalized = normalize_config(config)
    with tmp.open("w", encoding="utf-8") as f:
        yaml.safe_dump(normalized, f, sort_keys=False)
    os.replace(tmp, CONFIG_PATH)


def public_config(config):
    safe = json.loads(json.dumps(config))
    safe.get("dashboard", {}).pop("password", None)
    safe.get("dashboard", {}).pop("session_token", None)
    safe.get("arangodb", {}).pop("password", None)
    return safe


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
    config = load_config()
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if body.get("password") != config.get("dashboard", {}).get("password"):
        return JSONResponse({"ok": False, "error": "bad password"}, status_code=401)
    token = config.get("dashboard", {}).get("session_token", "")
    response = JSONResponse({"ok": True, "token": token})
    secure_cookie = request.url.scheme == "https"
    response.set_cookie("obbystreams_token", token, httponly=False, secure=secure_cookie, samesite="strict", max_age=60 * 60 * 24 * 30)
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


def hls_metrics(config):
    stream = config.get("stream", {})
    output_dir = Path(stream.get("output_dir", "/var/www/live.obnoxious.lol/stream"))
    playlist = output_dir / "ufc.m3u8"
    segments = [Path(p) for p in glob.glob(str(output_dir / "ufc*.ts"))]
    total_bytes = sum(safe_stat_size(p) for p in segments)
    playlist_age = None
    playlist_mtime = None
    playlist_lines = []
    target_duration = None
    media_sequence = None
    playlist_segment_names = []
    segment_durations = []
    segment_mtimes = [safe_stat_mtime(p) for p in segments]
    segment_mtimes = [m for m in segment_mtimes if m is not None]
    if playlist.exists():
        playlist_mtime = playlist.stat().st_mtime
        playlist_age = max(0, time.time() - playlist_mtime)
        try:
            playlist_lines = playlist.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            playlist_lines = []
    for line in playlist_lines:
        if line.startswith("#EXT-X-TARGETDURATION:"):
            target_duration = line.split(":", 1)[1]
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            media_sequence = line.split(":", 1)[1]
        elif line.startswith("#EXTINF:"):
            with contextlib.suppress(ValueError):
                segment_durations.append(float(line.split(":", 1)[1].split(",", 1)[0]))
        elif line and not line.startswith("#"):
            playlist_segment_names.append(line)
    return {
        "output_dir": str(output_dir),
        "playlist": str(playlist),
        "playlist_exists": playlist.exists(),
        "playlist_ready": bool(playlist_segment_names),
        "playlist_age": playlist_age,
        "playlist_modified_at": int(playlist_mtime * 1000) if playlist_mtime else None,
        "playlist_line_count": len(playlist_lines),
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
        "public_hls_url": stream.get("public_hls_url"),
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
    payload = {
        "ok": True,
        "config": public_config(config),
        "managed_process": proc,
        "existing_processes": stream_processes(),
        "hls": hls,
        "health": stream_health(config, proc, hls),
        "events": list(EVENTS)[-80:],
        "logs": list(LOGS)[-140:],
        "errors": list(ERRORS)[-80:],
        "server_time": now_ms(),
        "runtime": {
            **RUNTIME,
            "app_started_at": APP_STARTED_AT,
            "app_uptime_seconds": round(max(0.0, time.time() - (APP_STARTED_AT / 1000)), 2) if APP_STARTED_AT else None,
            "arango_queue_depth": ARANGO_QUEUE.qsize() if ARANGO_QUEUE else 0,
        },
    }
    queue_arango_insert("metrics", {"ts": now_ms(), "payload": payload})
    return payload


async def status(request):
    return JSONResponse(status_payload())


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
    if "links" in body:
        links = body["links"]
        if not isinstance(links, list):
            return JSONResponse({"ok": False, "error": "links must be an array"}, status_code=400)
        stream["links"] = normalize_links(links)
    for key in (
        "encoder",
        "bitrate",
        "audio_bitrate",
        "output_dir",
        "ffmpeg_log_dir",
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
        "encoder",
        "bitrate",
        "audio_bitrate",
        "output_dir",
        "ffmpeg_log_dir",
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
    links = config.setdefault("stream", {}).setdefault("links", [])
    if url not in links:
        links.append(url)
    config["stream"]["links"] = normalize_links(links)
    save_config(config)
    event("link added", "ok", {"url": url})
    queue_arango_insert("links", {"ts": now_ms(), "action": "add", "url": url})
    restarted = await restart_managed_with_config("link added")
    if restarted:
        event("running stream picked up updated links", "ok")
    return JSONResponse({"ok": True, "links": config["stream"]["links"]})


async def remove_link(request):
    config = load_config()
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    url = str(body.get("url", "")).strip()
    links = config.setdefault("stream", {}).setdefault("links", [])
    config["stream"]["links"] = [x for x in links if x != url]
    save_config(config)
    event("link removed", "warn", {"url": url})
    queue_arango_insert("links", {"ts": now_ms(), "action": "remove", "url": url})
    restarted = await restart_managed_with_config("link removed")
    if restarted:
        event("running stream picked up updated links", "ok")
    return JSONResponse({"ok": True, "links": config["stream"]["links"]})


async def read_process_output(proc):
    assert proc.stdout is not None
    while True:
        line = await asyncio.to_thread(proc.stdout.readline)
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
    links = links or stream.get("links", [])
    cmd = [stream.get("command", "/usr/bin/obbystreams"), "--no-color"]
    encoder = stream.get("encoder", "auto")
    if encoder:
        cmd += ["--encoder", encoder]
    if stream.get("output_dir"):
        cmd += ["--output-dir", stream["output_dir"]]
    if stream.get("ffmpeg_log_dir"):
        cmd += ["--ffmpeg-log-dir", stream["ffmpeg_log_dir"]]
    if stream.get("bitrate"):
        cmd += ["--bitrate", str(stream["bitrate"])]
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


async def stop_managed_process(reason):
    global PROCESS, READER_TASK
    proc = PROCESS
    if not proc or proc.poll() is not None:
        PROCESS = None
        STREAM_HEALTH_SCORER.reset()
        return False
    await asyncio.to_thread(terminate_process_tree, proc)
    if READER_TASK and not READER_TASK.done():
        READER_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await READER_TASK
    RUNTIME["last_exit_code"] = proc.poll()
    PROCESS = None
    STREAM_HEALTH_SCORER.reset()
    event(reason, "warn")
    return True


def start_managed_process(config, links, kill_existing=True):
    global PROCESS, STARTED_AT, READER_TASK
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
        links = config.get("stream", {}).get("links", [])
        try:
            start_managed_process(config, links, kill_existing=True)
            RUNTIME["stream_restarts"] += 1
        except (OSError, ValueError) as exc:
            event(f"stream restart failed: {exc}", "bad")
            ERRORS.append({"ts": now_ms(), "level": "error", "line": f"stream restart failed: {exc}"})
            return False
        return True


async def start_stream(request):
    global PROCESS
    try:
        body = await parse_json_body(request)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    config = load_config()
    raw_links = body.get("links")
    if raw_links is not None and not isinstance(raw_links, list):
        return JSONResponse({"ok": False, "error": "links must be an array"}, status_code=400)
    links = normalize_links(raw_links) if raw_links is not None else config.get("stream", {}).get("links", [])
    async with PROCESS_LOCK:
        if PROCESS and PROCESS.poll() is None:
            return JSONResponse({"ok": False, "error": "managed stream already running"}, status_code=409)
        try:
            pid, cmd = start_managed_process(config, links, kill_existing=body.get("kill_existing", True))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except OSError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, "pid": pid, "cmd": cmd})


async def stop_stream(request):
    async with PROCESS_LOCK:
        stopped = await stop_managed_process("stream stopped")
        return JSONResponse({"ok": True, "stopped": stopped})


async def restart_stream(request):
    async with PROCESS_LOCK:
        if PROCESS and PROCESS.poll() is None:
            await stop_managed_process("stream stopped")
    return await start_stream(request)


async def watchdog_loop():
    global WATCHDOG_LAST_ACTION, PROCESS
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
                    if stream.get("auto_restart_on_exit", True) and stream.get("links"):
                        now = time.monotonic()
                        if now - WATCHDOG_LAST_ACTION < restart_cooldown:
                            continue
                        WATCHDOG_LAST_ACTION = now
                        RUNTIME["watchdog_restarts"] += 1
                        event("watchdog restart: managed process exited", "warn")
                        try:
                            start_managed_process(config, stream.get("links", []), kill_existing=True)
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
                links = stream.get("links", [])
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
    public_hls_url = stream.get("public_hls_url", "")
    candidates = []
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
            if not any(line and not line.startswith("#") for line in text.splitlines()):
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
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), follow_redirects=True, headers={"User-Agent": "curl/8.0"}) as client:
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
    global WATCHDOG_TASK, ARANGO_QUEUE, ARANGO_WORKER_TASK
    ARANGO_QUEUE = asyncio.Queue(maxsize=ARANGO_QUEUE_MAX)
    ARANGO_WORKER_TASK = asyncio.create_task(arango_worker_loop())
    event("obbystreams dashboard booted", "ok")
    WATCHDOG_TASK = asyncio.create_task(watchdog_loop())
    try:
        yield
    finally:
        if WATCHDOG_TASK:
            WATCHDOG_TASK.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await WATCHDOG_TASK
        async with PROCESS_LOCK:
            await stop_managed_process("stream stopped during shutdown")
        if ARANGO_WORKER_TASK:
            ARANGO_WORKER_TASK.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ARANGO_WORKER_TASK


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
    Route("/api/config", guarded(get_config), methods=["GET"]),
    Route("/api/config", guarded(put_config), methods=["PUT"]),
    Route("/api/links", guarded(add_link), methods=["POST"]),
    Route("/api/links/remove", guarded(remove_link), methods=["POST"]),
    Route("/api/stream/start", guarded(start_stream), methods=["POST"]),
    Route("/api/stream/stop", guarded(stop_stream), methods=["POST"]),
    Route("/api/stream/restart", guarded(restart_stream), methods=["POST"]),
    Route("/api/arango", guarded(arango_status)),
    Route("/api/nvidia-smi", guarded(nvidia_smi_status)),
    Route("/hls/{path:path}", hls_proxy),
    Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
]

app = Starlette(debug=False, routes=routes, lifespan=lifespan)
