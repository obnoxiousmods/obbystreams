#!/usr/bin/env python3
"""Watch what is actually ON SCREEN and rotate away from a provider slate.

Runs beside the service (no restart needed). Samples the published output every
--period seconds via content_guard; a slate must be seen --confirm times in a row
before anything happens, and rotations are capped, because an unnecessary switch
costs ~10s of picture while a missed slate costs the whole card.
"""
import argparse
import contextlib
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

GUARD = Path(__file__).with_name("content_guard.py")


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def api(path, token, payload=None):
    url = f"http://127.0.0.1:8767{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("x-obbystreams-token", token)
    if data:
        req.add_header("content-type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def sample(source, frames, interval):
    out = subprocess.run(
        [sys.executable, str(GUARD), "--source", source, "--frames", str(frames),
         "--interval", str(interval), "--json"],
        capture_output=True, text=True, timeout=frames * interval + 90,
    )
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        return {"state": "unknown", "reason": out.stderr[-200:]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="/var/www/live.obnoxious.lol/stream/media_1.m3u8")
    ap.add_argument("--period", type=float, default=30.0)
    ap.add_argument("--frames", type=int, default=6)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--confirm", type=int, default=2)
    ap.add_argument("--max-rotations", type=int, default=3, help="per --rotation-window minutes")
    ap.add_argument("--rotation-window", type=float, default=30.0)
    ap.add_argument("--min-encode-age", type=float, default=60.0)
    ap.add_argument("--act", action="store_true", help="rotate sources; otherwise alert only")
    ap.add_argument("--minutes", type=float, default=180.0)
    args = ap.parse_args()

    token = ""
    for line in Path("/etc/obbystreams/obbystreams.yaml").read_text().splitlines():
        if "session_token:" in line:
            token = line.split(":", 1)[1].strip()
            break

    deadline = time.time() + args.minutes * 60
    streak = 0
    # A ROLLING window, not a lifetime budget. A lifetime cap sounds safe and is
    # not: on 2026-08-29 it was spent by 04:03 and the guard then watched a slate
    # sit on air for seven minutes announcing that it would do nothing about it,
    # with the main event still to come. The cap exists to stop a rotation LOOP,
    # which is a burst, so it has to forget.
    rotation_times = []
    known_bad: set[str] = set()
    log(f"content watch started (act={args.act}, confirm={args.confirm}, cap={args.max_rotations})")
    while time.time() < deadline:
        r = sample(args.source, args.frames, args.interval)
        state = r.get("state")
        # Log EVERY sample. A watchdog that only speaks up when something is wrong
        # is indistinguishable from a watchdog that has died.
        rotation_times[:] = [t for t in rotation_times if time.time() - t < args.rotation_window * 60]
        log(f"{state:15} motion min={r.get('motion_min')} mean={r.get('motion_mean')} "
            f"rotations={len(rotation_times)}/{args.max_rotations} in {args.rotation_window:.0f}m"
            + (f" | {r.get('ocr_kind')}" if r.get("ocr_kind") else ""))
        if state == "live":
            streak = 0
        # Only a CONFIRMED provider fault is actionable. "static-benign" is an ad
        # break and "static-unknown" is a still picture we cannot explain -- both
        # are reasons to wait, not to interrupt viewers.
        if state != "slate":
            time.sleep(args.period)
            continue

        streak += 1
        if streak < args.confirm:
            time.sleep(args.period)
            continue

        log(f"*** SLATE CONFIRMED ({streak} consecutive samples) ***")
        # Only the case this guard EXISTS for: encode green, content wrong. While
        # the encoder is unhealthy or seconds old its own link ladder owns the
        # problem, and rotating on top of that is pure interference -- on
        # 2026-08-29 the guard fired three times inside the main-card acquisition
        # storm (repeated "Error opening input", "4 restarts in 15m") and just
        # added churn to links the ladder was already walking.
        try:
            st = api("/api/status", token)
            decision = str(st.get("health", {}).get("decision") or "").lower()
            age = float(st.get("managed_process", {}).get("age") or 0)
        except Exception as exc:
            log(f"could not read health ({exc}); not rotating")
            time.sleep(args.period)
            continue
        if decision != "healthy" or age < args.min_encode_age:
            log(f"encoder is {decision} and {age:.0f}s old; its own ladder owns this. Not rotating.")
            time.sleep(args.period)
            continue
        if not args.act:
            log("alert-only mode; not rotating")
            streak = 0
            time.sleep(args.period)
            continue
        if len(rotation_times) >= args.max_rotations:
            oldest = min(rotation_times)
            free_in = args.rotation_window * 60 - (time.time() - oldest)
            log(f"rotation cap {args.max_rotations}/{args.rotation_window:.0f}m reached; "
                f"budget frees up in {max(0, free_in) / 60:.1f}m. Alerting only.")
            time.sleep(args.period)
            continue

        try:
            current = ""
            with contextlib.suppress(Exception):
                current = json.loads(Path("/var/www/live.obnoxious.lol/stream/.encode-progress.json").read_text())["link_url"]
            if current:
                known_bad.add(current)
            links = st.get("config", {}).get("stream", {}).get("links") or []
            # Skip links already caught serving a slate. Picking "the first link
            # that is not the current one" ping-ponged 700019613 <-> 700019663
            # three times in four minutes on 2026-08-29, burning the whole budget
            # on two links that were both dead.
            nxt = next((u for u in links if u not in known_bad), None)
            if not nxt:
                log(f"every link ({len(links)}) has served a slate; clearing the memory and starting over")
                known_bad.clear()
                nxt = next((u for u in links if u != current), None)
            if not nxt:
                log("no alternative link to rotate to")
                time.sleep(args.period)
                continue
            log(f"rotating {current.rsplit('/',1)[-1]} -> {nxt.rsplit('/',1)[-1]}")
            api("/api/sources/activate", token, {"url": nxt})
            rotation_times.append(time.time())
            streak = 0
            time.sleep(60)  # let the new encode establish before judging it
        except Exception as exc:
            log(f"rotation failed: {exc}")
            time.sleep(args.period)
    log("content watch finished")


if __name__ == "__main__":
    main()
