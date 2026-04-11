#!/usr/bin/env python3
import asyncio
import base64
import contextlib
import glob
import json
import os
import signal
import subprocess
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

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

EVENTS: deque[dict] = deque(maxlen=300)
LOGS: deque[dict] = deque(maxlen=600)
PROCESS = None
STARTED_AT = None
READER_TASK = None


def now_ms():
    return int(time.time() * 1000)


def load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_config(config):
    tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, sort_keys=False)
    os.replace(tmp, CONFIG_PATH)


def public_config(config):
    safe = json.loads(json.dumps(config))
    safe.get("dashboard", {}).pop("password", None)
    safe.get("dashboard", {}).pop("session_token", None)
    safe.get("arangodb", {}).pop("password", None)
    return safe


def event(message, level="info", extra=None):
    item = {"ts": now_ms(), "level": level, "message": message, "extra": extra or {}}
    EVENTS.append(item)
    asyncio.create_task(arango_insert("events", item))
    return item


def require_auth(request):
    config = load_config()
    token = config.get("dashboard", {}).get("session_token", "")
    if not token:
        return True
    supplied = request.headers.get("x-obbystreams-token", "") or request.cookies.get("obbystreams_token", "")
    return supplied == token


def guarded(handler):
    async def wrapped(request):
        if not require_auth(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        return await handler(request)
    return wrapped


async def login(request):
    config = load_config()
    body = await request.json()
    if body.get("password") != config.get("dashboard", {}).get("password"):
        return JSONResponse({"ok": False, "error": "bad password"}, status_code=401)
    token = config.get("dashboard", {}).get("session_token", "")
    response = JSONResponse({"ok": True, "token": token})
    response.set_cookie("obbystreams_token", token, httponly=False, secure=True, samesite="strict", max_age=60 * 60 * 24 * 30)
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
        return response.json()


async def arango_insert(collection, doc):
    try:
        return await arango_request("POST", f"/_api/document/{collection}", doc)
    except Exception:
        return None


async def arango_status(request):
    try:
        data = await arango_request("GET", "/_api/version")
        return JSONResponse({"ok": True, "connected": True, "version": data})
    except Exception as exc:
        return JSONResponse({"ok": True, "connected": False, "error": str(exc)})


def stream_processes():
    found = []
    current_pid = os.getpid()
    for proc in psutil.process_iter(["pid", "cmdline", "create_time", "name"]):
        try:
            if proc.info["pid"] == current_pid:
                continue
            cmdline = proc.info.get("cmdline") or []
            cmd = " ".join(cmdline)
            if (
                "/usr/bin/obbystreams" in cmdline
                or "/usr/bin/obbystreams" in cmd
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
    gone, alive = psutil.wait_procs([psutil.Process(p["pid"]) for p in killed if psutil.pid_exists(p["pid"])], timeout=2)
    for proc in alive:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.kill()
    return killed


def hls_metrics(config):
    stream = config.get("stream", {})
    output_dir = Path(stream.get("output_dir", "/var/www/live.obnoxious.lol/stream"))
    playlist = output_dir / "ufc.m3u8"
    segments = [Path(p) for p in glob.glob(str(output_dir / "ufc*.ts"))]
    total_bytes = sum(p.stat().st_size for p in segments if p.exists())
    playlist_age = None
    playlist_mtime = None
    playlist_lines = []
    target_duration = None
    media_sequence = None
    playlist_segment_names = []
    segment_durations = []
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
        "playlist_age": playlist_age,
        "playlist_modified_at": int(playlist_mtime * 1000) if playlist_mtime else None,
        "playlist_line_count": len(playlist_lines),
        "segments": len(segments),
        "bytes": total_bytes,
        "target_duration": target_duration,
        "media_sequence": media_sequence,
        "segment_window_seconds": round(sum(segment_durations), 3),
        "playlist_segments": playlist_segment_names[-12:],
        "first_segment": playlist_segment_names[0] if playlist_segment_names else None,
        "last_segment": playlist_segment_names[-1] if playlist_segment_names else None,
        "last_segment_size": (output_dir / playlist_segment_names[-1]).stat().st_size if playlist_segment_names and (output_dir / playlist_segment_names[-1]).exists() else None,
        "public_hls_url": stream.get("public_hls_url"),
        "dashboard_hls_url": "/hls/ufc.m3u8",
    }


def process_metrics():
    global PROCESS, STARTED_AT
    pid = PROCESS.pid if PROCESS and PROCESS.poll() is None else None
    data = {"managed": bool(pid), "pid": pid, "started_at": STARTED_AT, "cpu": None, "rss": None, "children": []}
    if not pid:
        return data
    try:
        proc = psutil.Process(pid)
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


def status_payload():
    config = load_config()
    payload = {
        "ok": True,
        "config": public_config(config),
        "managed_process": process_metrics(),
        "existing_processes": stream_processes(),
        "hls": hls_metrics(config),
        "events": list(EVENTS)[-80:],
        "logs": list(LOGS)[-140:],
        "server_time": now_ms(),
    }
    asyncio.create_task(arango_insert("metrics", {"ts": now_ms(), "payload": payload}))
    return payload


async def status(request):
    return JSONResponse(status_payload())


async def get_config(request):
    return JSONResponse({"ok": True, "config": public_config(load_config())})


async def put_config(request):
    config = load_config()
    body = await request.json()
    stream = config.setdefault("stream", {})
    if "links" in body:
        stream["links"] = [str(x).strip() for x in body["links"] if str(x).strip()]
    for key in ("encoder", "bitrate", "audio_bitrate", "output_dir", "public_hls_url"):
        if key in body:
            stream[key] = body[key]
    save_config(config)
    event("configuration updated", "ok", {"keys": list(body.keys())})
    await arango_insert("configs", {"ts": now_ms(), "config": public_config(config)})
    return JSONResponse({"ok": True, "config": public_config(config)})


async def add_link(request):
    config = load_config()
    body = await request.json()
    url = str(body.get("url", "")).strip()
    if not url:
        return JSONResponse({"ok": False, "error": "url required"}, status_code=400)
    links = config.setdefault("stream", {}).setdefault("links", [])
    if url not in links:
        links.append(url)
    save_config(config)
    event("link added", "ok", {"url": url})
    await arango_insert("links", {"ts": now_ms(), "action": "add", "url": url})
    return JSONResponse({"ok": True, "links": links})


async def remove_link(request):
    config = load_config()
    body = await request.json()
    url = str(body.get("url", "")).strip()
    links = config.setdefault("stream", {}).setdefault("links", [])
    config["stream"]["links"] = [x for x in links if x != url]
    save_config(config)
    event("link removed", "warn", {"url": url})
    await arango_insert("links", {"ts": now_ms(), "action": "remove", "url": url})
    return JSONResponse({"ok": True, "links": config["stream"]["links"]})


async def read_process_output(proc):
    assert proc.stdout is not None
    while True:
        line = await asyncio.to_thread(proc.stdout.readline)
        if not line:
            break
        line = line.rstrip()
        if line:
            LOGS.append({"ts": now_ms(), "line": line})
            if "ffmpeg exited" in line or "starting" in line or "restart" in line:
                event(line, "info")


def build_command(config, links=None):
    stream = config.get("stream", {})
    links = links or stream.get("links", [])
    cmd = [stream.get("command", "/usr/bin/obbystreams"), "--no-color"]
    encoder = stream.get("encoder", "auto")
    if encoder:
        cmd += ["--encoder", encoder]
    if stream.get("output_dir"):
        cmd += ["--output-dir", stream["output_dir"]]
    if stream.get("bitrate"):
        cmd += ["--bitrate", str(stream["bitrate"])]
    if stream.get("audio_bitrate"):
        cmd += ["--audio-bitrate", str(stream["audio_bitrate"])]
    if links:
        cmd += ["--links", *links]
    return cmd


async def start_stream(request):
    global PROCESS, STARTED_AT, READER_TASK
    if PROCESS and PROCESS.poll() is None:
        return JSONResponse({"ok": False, "error": "managed stream already running"}, status_code=409)
    body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    config = load_config()
    links = body.get("links") or config.get("stream", {}).get("links", [])
    if not links:
        return JSONResponse({"ok": False, "error": "no links configured"}, status_code=400)
    if body.get("kill_existing", True):
        killed = kill_existing_streams()
        if killed:
            event("killed existing stream instance(s)", "warn", {"processes": killed})
    cmd = build_command(config, links)
    PROCESS = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    STARTED_AT = now_ms()
    READER_TASK = asyncio.create_task(read_process_output(PROCESS))
    event("stream started", "ok", {"cmd": cmd, "pid": PROCESS.pid})
    return JSONResponse({"ok": True, "pid": PROCESS.pid, "cmd": cmd})


async def stop_stream(request):
    global PROCESS
    if not PROCESS or PROCESS.poll() is not None:
        return JSONResponse({"ok": True, "stopped": False})
    PROCESS.terminate()
    try:
        await asyncio.wait_for(asyncio.to_thread(PROCESS.wait), timeout=5)
    except TimeoutError:
        PROCESS.kill()
    event("stream stopped", "warn")
    return JSONResponse({"ok": True, "stopped": True})


async def restart_stream(request):
    await stop_stream(request)
    return await start_stream(request)


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
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), follow_redirects=True, headers={"User-Agent": "curl/8.0"}) as client:
            for remote_url in upstream_urls:
                response = await client.get(remote_url)
                last_response = response
                if response.status_code < 400:
                    break
            else:
                assert last_response is not None
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


@asynccontextmanager
async def lifespan(app):
    event("obbystreams dashboard booted", "ok")
    try:
        yield
    finally:
        if PROCESS and PROCESS.poll() is None:
            PROCESS.send_signal(signal.SIGTERM)


routes = [
    Route("/", index),
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
    Route("/hls/{path:path}", hls_proxy),
    Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
]

app = Starlette(debug=False, routes=routes, lifespan=lifespan)
