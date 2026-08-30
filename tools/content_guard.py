#!/usr/bin/env python3
"""Decide whether the encode is carrying a live broadcast or a provider slate.

Every existing health signal answers "are bytes flowing", never "what are they".
On 2026-08-29 that gap put a "CONNECTION LIMIT REACHED" card on air for 24
unbroken minutes at 1.00x, 0 dropped frames, playlist fresh, health "healthy",
delivered flawlessly at 60 segments/minute -- and later a "This channel is
currently offline" card while the cockpit read `0.88x / RUN healthy / ok: healthy`.

The only ground truth is the pixels, and exactly one property separates them:

  motion   a slate is a still card; a live broadcast never stops moving.
           Measured 2026-08-29 on this stream: live fight = 54.1 mean
           inter-frame delta (28.4 at its quietest), a still card = 0.

COLOUR WAS TRIED AND REJECTED -- do not add it back. It seems obvious that a
slate is drab, and it is dead wrong: the "CONNECTION LIMIT REACHED" card scored
58.6 mean saturation against the real fight's 36.5, because it is mostly a large
red banner. Gating on "still AND colourless" would have sailed straight past the
exact slate that played for 24 minutes. Only "This channel is currently offline"
(pure greyscale, 0.0) would have been caught.

A single still frame is not enough evidence -- a fighter can stand motionless
between exchanges -- so every sampled delta across the window must be below the
floor before this calls it. Note that a genuinely frozen upstream also trips
this, which is correct: a picture that has not moved in fifteen seconds is a
fault either way.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

#: A static picture is not automatically a fault. Paramount+ shows a still
#: "Commercial in Progress" card through every ad break, and the UFC feed cuts to
#: static promos -- rotating away from those is worse than useless, because every
#: other link carries the SAME broadcast and will show the same card. Only these
#: mean the PROVIDER is failing us and another link might not be.
FAULT_PATTERNS = re.compile(
    r"connection limit|all slots|simultaneous connection|too many|currently offline"
    r"|no live event|choose another channel|stream not available|subscription",
    re.I,
)
#: Seen and known harmless. Matched only to log a clear reason; anything
#: unrecognised is treated as benign anyway.
BENIGN_PATTERNS = re.compile(r"commercial|advertisement|be right back|paramount|espn|fight pass", re.I)


def read_text(path):
    """OCR a frame. Returns "" when tesseract is missing or fails -- callers must
    treat that as 'unknown', never as 'fault'."""
    try:
        out = subprocess.run(
            ["tesseract", str(path), "stdout", "--psm", "6"],
            capture_output=True, text=True, timeout=25,
        )
        return " ".join(out.stdout.split())
    except (OSError, subprocess.SubprocessError):
        return ""


def classify_static(path):
    """Why is the picture static? Returns (kind, text) where kind is
    'provider-fault' | 'broadcast' | 'unknown'.

    Defaults to NOT a fault. A false 'fault' rotates a working stream; a false
    'benign' just means we wait, which is what a human would do anyway.
    """
    text = read_text(path)
    if FAULT_PATTERNS.search(text):
        return "provider-fault", text[:160]
    if BENIGN_PATTERNS.search(text):
        return "broadcast", text[:160]
    return "unknown", text[:160]


def grab_full_frame(source, workdir):
    """One frame at NATIVE resolution, for OCR.

    Motion detection wants small frames (cheap, and downscaling suppresses codec
    noise); OCR wants every pixel it can get. Sharing one downscaled frame
    between them silently broke classification: at 320px wide the
    "CONNECTION LIMIT REACHED" card's body text is unreadable and tesseract
    returned an empty string, so the single most important slate in the system
    classified as "unknown" -- i.e. not a fault -- while the Paramount+ ad card
    still read fine because its lettering is enormous. Unit tests missed it
    because they feed full-size fixtures straight to classify_static.
    """
    out = Path(workdir) / "ocr.png"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(source), "-frames:v", "1", str(out)],
        check=False, timeout=60,
    )
    return out if out.exists() else None


def grab(source, count, interval, workdir):
    """Pull `count` frames `interval` seconds apart from a live playlist."""
    out = Path(workdir) / "f_%03d.png"
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-i", source,
        "-vf", f"fps=1/{interval},scale=320:-1",
        "-frames:v", str(count),
        str(out),
    ]
    subprocess.run(cmd, check=False, timeout=count * interval + 45)
    return sorted(Path(workdir).glob("f_*.png"))


def measure(paths):
    frames = [np.asarray(Image.open(p).convert("RGB"), dtype=np.float32) for p in paths]
    if len(frames) < 2:
        return None
    deltas = [float(np.abs(frames[i] - frames[i - 1]).mean()) for i in range(1, len(frames))]
    return {
        "frames": len(frames),
        "motion_mean": round(sum(deltas) / len(deltas), 3),
        "motion_max": round(max(deltas), 3),
        "motion_min": round(min(deltas), 3),
    }


def verdict(m, motion_floor):
    if m is None:
        return "unknown", "could not sample enough frames"
    # EVERY delta must be below the floor. Using the mean would let one cut or
    # camera move mask a window that is otherwise frozen.
    if m["motion_max"] < motion_floor:
        return "slate", f"picture never moved across {m['frames']} frames (max delta {m['motion_max']} < {motion_floor})"
    if m["motion_min"] < motion_floor:
        return "suspect", f"picture froze for part of the window (min delta {m['motion_min']})"
    return "live", f"moving throughout (min delta {m['motion_min']}, mean {m['motion_mean']})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="/var/www/live.obnoxious.lol/stream/media_1.m3u8")
    ap.add_argument("--frames", type=int, default=6)
    ap.add_argument("--interval", type=float, default=2.0)
    # Live measured 28.4 at its quietest and a still card is 0, so 3.0 sits an
    # order of magnitude clear of both.
    ap.add_argument("--motion-floor", type=float, default=3.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    kind, text = "", ""
    with tempfile.TemporaryDirectory() as tmp:
        paths = grab(args.source, args.frames, args.interval, tmp)
        m = measure(paths)
        state, why = verdict(m, args.motion_floor)
        if state == "slate":
            ocr_frame = grab_full_frame(args.source, tmp) or (paths[-1] if paths else None)
            kind, text = classify_static(ocr_frame) if ocr_frame else ("unknown", "")
            if kind != "provider-fault":
                # Static, but not OUR fault. An ad break is not a reason to
                # interrupt anyone.
                state = "static-benign" if kind == "broadcast" else "static-unknown"
                why = f"{why}; OCR says {kind}: {text[:80]!r}"
            else:
                why = f"{why}; OCR confirms provider fault: {text[:80]!r}"
    payload = {"state": state, "reason": why, "ocr_kind": kind, "ocr_text": text, **(m or {})}
    print(json.dumps(payload) if args.json else f"{state}: {why}  {m}")
    return 0 if state == "live" else (2 if state == "slate" else 1)


if __name__ == "__main__":
    sys.exit(main())
