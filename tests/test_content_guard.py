"""Is the picture on air actually a broadcast, or a provider slate?

Every other health signal answers "are bytes flowing", never "what are they". On
2026-08-29 a "CONNECTION LIMIT REACHED" card played to viewers for 24 unbroken
minutes at 1.00x, 0 dropped frames, fresh playlist, health "healthy", delivered
at a flawless 60 segments/minute. Later a "This channel is currently offline"
card ran while the cockpit read `0.88x / RUN healthy / ok: healthy`.

The numbers below are MEASURED off this stream on that night, not invented.
"""

import json
import runpy
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

GUARD = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools" / "content_guard.py"))
measure = GUARD["measure"]
verdict = GUARD["verdict"]
FLOOR = 3.0

#: Measured 2026-08-29 on the live Nurmagomedov vs. Song feed.
LIVE_MOTION_MIN = 24.1
#: Measured on the two real slates, re-encoded from captured frames.
CONNECTION_LIMIT_SLATE_MAX = 0.264
OFFLINE_SLATE_MAX = 0.004


def _m(deltas):
    """Build a measure() result from a known delta series."""
    return {
        "frames": len(deltas) + 1,
        "motion_mean": sum(deltas) / len(deltas),
        "motion_max": max(deltas),
        "motion_min": min(deltas),
    }


def test_the_live_fight_reads_as_live():
    state, _ = verdict(_m([54.1, 28.4, 64.3, LIVE_MOTION_MIN]), FLOOR)
    assert state == "live"


def test_the_connection_limit_slate_is_caught():
    """The one that ran for 24 minutes."""
    state, why = verdict(_m([0.0, 0.11, CONNECTION_LIMIT_SLATE_MAX, 0.03]), FLOOR)
    assert state == "slate"
    assert "never moved" in why


def test_the_offline_slate_is_caught():
    state, _ = verdict(_m([0.0, 0.004, OFFLINE_SLATE_MAX, 0.0]), FLOOR)
    assert state == "slate"


def test_the_threshold_keeps_an_order_of_magnitude_either_side():
    """If someone retunes FLOOR, this is the margin they are spending."""
    assert CONNECTION_LIMIT_SLATE_MAX * 10 < FLOOR < LIVE_MOTION_MIN / 5


def test_a_momentary_freeze_is_suspect_not_a_slate():
    """Fighters do stand still. One frozen sample must not tear down the feed --
    only a window that never moved at all counts."""
    state, _ = verdict(_m([31.0, 0.2, 44.0]), FLOOR)
    assert state == "suspect"


def test_colour_is_not_consulted():
    """Colour was tried and REJECTED and must not come back. The CONNECTION LIMIT
    slate measured 58.6 mean saturation against the real fight's 36.5 -- it is
    mostly a big red banner -- so gating on 'still AND colourless' would have
    sailed straight past the exact slate that cost 24 minutes."""
    assert "colour" not in str(GUARD["verdict"].__doc__ or "").lower()
    m = _m([0.0, 0.0, 0.0])
    assert "colour" not in verdict(m, FLOOR)[1].lower()


def test_measure_reports_the_quietest_transition_not_just_the_average():
    a = np.zeros((8, 8, 3), dtype=np.uint8)
    b = np.full((8, 8, 3), 40, dtype=np.uint8)
    from PIL import Image

    with pytest.MonkeyPatch.context():
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i, arr in enumerate([a, b, b]):
                p = Path(tmp) / f"f_{i:03d}.png"
                Image.fromarray(arr).save(p)
                paths.append(p)
            m = measure(paths)
    # a->b moved by 40, b->b did not move at all. The mean would hide the freeze.
    assert m["motion_max"] == pytest.approx(40.0, abs=0.5)
    assert m["motion_min"] == pytest.approx(0.0, abs=0.5)


def test_too_few_frames_is_unknown_not_a_verdict():
    assert verdict(None, FLOOR)[0] == "unknown"


# --- classifying a static picture -------------------------------------------
#
# Motion alone says "the picture stopped moving". That is NOT the same as "the
# provider is failing us", and conflating the two is a real hazard: Paramount+
# shows a still "Commercial in Progress" card through every ad break, and every
# other link carries the SAME broadcast, so rotating during an ad is guaranteed to
# achieve nothing while costing viewers a restart. Caught 2026-08-29 at 04:54,
# after the guard had already been rotating on ad breaks.
#
# The fixtures are real frames captured off the live stream that night.

FRAMES = Path(__file__).parent / "fixtures" / "frames"
requires_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="tesseract not installed"
)


@requires_tesseract
@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        ("slate_connection_limit.png", "provider-fault"),
        ("slate_channel_offline.png", "provider-fault"),
        ("broadcast_commercial_break.png", "broadcast"),
    ],
)
def test_static_frames_are_classified_by_cause(frame, expected):
    kind, _text = GUARD["classify_static"](FRAMES / frame)
    assert kind == expected


@requires_tesseract
@pytest.mark.parametrize("frame", ["live_fight_jenkins.png", "live_fight_perez.png"])
def test_real_fight_frames_are_never_called_a_provider_fault(frame):
    kind, _text = GUARD["classify_static"](FRAMES / frame)
    assert kind != "provider-fault"


@requires_tesseract
def test_an_ad_break_must_not_read_as_a_provider_fault():
    """The regression that matters most: acting on this rotates a healthy stream
    away from a broadcast every other link is also showing."""
    kind, text = GUARD["classify_static"](FRAMES / "broadcast_commercial_break.png")
    assert kind == "broadcast"
    assert "commercial" in text.lower()


def test_unreadable_text_is_never_a_fault(monkeypatch):
    """OCR failing (missing binary, garbled frame) must degrade to 'do nothing'.
    A false 'fault' interrupts viewers; a false 'benign' just waits."""
    monkeypatch.setitem(GUARD, "read_text", lambda _p: "")
    src = GUARD["classify_static"].__globals__
    monkeypatch.setitem(src, "read_text", lambda _p: "")
    kind, _ = GUARD["classify_static"](FRAMES / "slate_connection_limit.png")
    assert kind == "unknown"


# --- content health overrules delivery health --------------------------------
import app  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_content_state():
    saved = dict(app.CONTENT_STATE)
    yield
    app.CONTENT_STATE.clear()
    app.CONTENT_STATE.update(saved)


def _set_content(**kw):
    app.CONTENT_STATE.clear()
    app.CONTENT_STATE.update({"checked_at": time.time(), **kw})


def test_a_confirmed_provider_fault_overrules_a_green_delivery_verdict(monkeypatch):
    """THE 2026-08-29 FAILURE. Delivery was flawless -- 1.00x, 0 dropped frames,
    fresh playlist, 60 segments a minute -- while the picture was a
    "CONNECTION LIMIT REACHED" card, for 24 unbroken minutes."""
    monkeypatch.setattr(app.STREAM_HEALTH_SCORER, "assess",
                        lambda *a, **k: {"decision": "healthy", "state": "healthy", "level": "ok"})
    _set_content(state="slate", fault_streak=2, ocr_kind="provider-fault",
                 ocr_text="CONNECTION LIMIT REACHED Your plan allows 2")

    doc = app.stream_health({}, {}, {})

    assert doc["decision"] == "failed"
    assert doc["state"] == "content-fault"
    assert "provider error card" in doc["message"]


def test_an_ad_break_does_not_overrule_anything(monkeypatch):
    """Paramount+ shows a still card through every ad break and every link
    carries the same broadcast. Failing health there would restart viewers for
    nothing, repeatedly, on a schedule set by the broadcaster."""
    monkeypatch.setattr(app.STREAM_HEALTH_SCORER, "assess",
                        lambda *a, **k: {"decision": "healthy", "state": "healthy", "level": "ok"})
    _set_content(state="static-benign", fault_streak=0, ocr_kind="broadcast",
                 ocr_text="Paramount + Commercial in Progress")

    assert app.stream_health({}, {}, {})["decision"] == "healthy"


def test_one_bad_sample_is_not_enough(monkeypatch):
    monkeypatch.setattr(app.STREAM_HEALTH_SCORER, "assess",
                        lambda *a, **k: {"decision": "healthy", "state": "healthy", "level": "ok"})
    _set_content(state="slate", fault_streak=1, ocr_kind="provider-fault", ocr_text="offline")

    assert app.stream_health({}, {}, {})["decision"] == "healthy"


def test_a_stalled_sampler_cannot_pin_health(monkeypatch):
    """If the content loop dies, its last verdict must expire rather than hold
    the stream down forever. A monitor that cannot fail safe is a liability."""
    monkeypatch.setattr(app.STREAM_HEALTH_SCORER, "assess",
                        lambda *a, **k: {"decision": "healthy", "state": "healthy", "level": "ok"})
    app.CONTENT_STATE.clear()
    app.CONTENT_STATE.update({
        "state": "slate", "fault_streak": 5, "ocr_kind": "provider-fault",
        "checked_at": time.time() - app.CONTENT_MAX_AGE_SECONDS - 60,
    })

    assert app.stream_health({}, {}, {})["decision"] == "healthy"
    assert app.content_state_snapshot()["stale"] is True


def test_health_always_carries_the_content_verdict(monkeypatch):
    """So the cockpit can show what is on screen, not just that bytes moved."""
    monkeypatch.setattr(app.STREAM_HEALTH_SCORER, "assess",
                        lambda *a, **k: {"decision": "healthy", "state": "healthy", "level": "ok"})
    _set_content(state="live", fault_streak=0, motion_mean=42.0)

    assert app.stream_health({}, {}, {})["content"]["state"] == "live"


# --- end-to-end: video in, verdict out ---------------------------------------
#
# The unit tests above feed full-size PNGs straight to classify_static, which
# skips the sampling pipeline entirely. That gap hid a real bug: grab() downscales
# to 320px for motion detection, and OCR was reading those same frames, so the
# "CONNECTION LIMIT REACHED" card -- the single most important slate in the
# system -- returned empty text and classified as "unknown" (i.e. not a fault).
# The Paramount+ ad card still read fine because its lettering is huge, which is
# exactly the sort of partial success that hides a defect. These tests run the
# whole path.

requires_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _still_video(frame_path, dest):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-loop", "1", "-i", str(frame_path),
         "-t", "16", "-r", "30", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", str(dest)],
        check=True, timeout=120,
    )
    return dest


def _run_guard(source):
    out = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "tools" / "content_guard.py"),
         "--source", str(source), "--json", "--frames", "5", "--interval", "2"],
        capture_output=True, text=True, timeout=180,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


@requires_ffmpeg
@requires_tesseract
def test_end_to_end_a_provider_slate_is_a_fault(tmp_path):
    video = _still_video(FRAMES / "slate_connection_limit.png", tmp_path / "cl.mp4")
    r = _run_guard(video)

    assert r["state"] == "slate"
    assert r["ocr_kind"] == "provider-fault"
    assert "simultaneous connections" in r["ocr_text"]


@requires_ffmpeg
@requires_tesseract
def test_end_to_end_an_ad_break_is_not_a_fault(tmp_path):
    video = _still_video(FRAMES / "broadcast_commercial_break.png", tmp_path / "ad.mp4")
    r = _run_guard(video)

    assert r["state"] == "static-benign"
    assert r["ocr_kind"] == "broadcast"


@requires_ffmpeg
def test_end_to_end_moving_video_is_live(tmp_path):
    dest = tmp_path / "live.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
         "-t", "16", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(dest)],
        check=True, timeout=120,
    )
    assert _run_guard(dest)["state"] == "live"
