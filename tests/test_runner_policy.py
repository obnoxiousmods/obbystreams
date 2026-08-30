import runpy
from pathlib import Path

RUNNER = runpy.run_path(str(Path(__file__).resolve().parents[1] / "bin" / "obbystreams"))


def test_runner_detects_private_capacity_errors():
    is_capacity_limited_error = RUNNER["is_capacity_limited_error"]

    assert is_capacity_limited_error("HTTP 429 too many requests")
    assert is_capacity_limited_error("provider says too many streams active")
    assert is_capacity_limited_error("concurrent connection limit exceeded")
    assert not is_capacity_limited_error("404 not found")


# --- slate detection ---------------------------------------------------------
#
# 2026-08-29. The provider served a "CONNECTION LIMIT REACHED" slate instead of
# the fight, and it played to viewers for 24 unbroken minutes at a flawless 60
# segments/minute. Nothing caught it because a slate is VIDEO: speed=1x, 0
# dropped frames, fresh playlist, health "healthy". The one thing that IS loud is
# ffmpeg's reaction to a looping clip -- the same PTS range replayed forever.
# Measured: 497,461 log lines in 24 minutes (~350/sec) on the slate, and 0/sec on
# the real feed that replaced it.

SLATE_LINE = "[aost#0:2/aac @ 0x55] Non-monotonic DTS; previous: 26036224, current: 0; changing to 26036225"
REAL_LINES = [
    "[aist#0:1/aac @ 0x55] Resumed reading at pts 4.011 with rate 2.000 after a lag of 1.194s",
    "[h264 @ 0x55] non-existing PPS 0 referenced",
    "[https @ 0x55] Will reconnect at 716844 in 1 second(s), error=End of file.",
]


class _FakeProc:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True


def _slate_state():
    return {"lock": __import__("threading").RLock(), "proc_obj": _FakeProc()}


def _feed(state, line, count, span_seconds, monkeypatch):
    """Push `count` copies of `line` spread over `span_seconds` of monotonic time."""
    record = RUNNER["record_slate_signature"]
    clock = {"t": 1000.0}
    monkeypatch.setattr(RUNNER["time"], "monotonic", lambda: clock["t"])
    step = span_seconds / max(1, count)
    for _ in range(count):
        record(state, line)
        clock["t"] += step


def test_a_looping_slate_is_detected_and_kills_the_encode(monkeypatch):
    state = _slate_state()
    # 350 lines/sec for 30s -- the rate actually observed on the slate.
    _feed(state, SLATE_LINE, 10_500, 30.0, monkeypatch)

    assert state.get("slate_kill") is True
    assert state["slate_kill_rate"] > RUNNER["SLATE_LINES_PER_SEC"]
    assert state["proc_obj"].terminated is True


def test_a_real_feed_is_never_mistaken_for_a_slate(monkeypatch):
    state = _slate_state()
    for line in REAL_LINES:
        _feed(state, line, 200, 120.0, monkeypatch)

    assert not state.get("slate_kill")
    assert state["proc_obj"].terminated is False


def test_an_occasional_bad_packet_does_not_trip_it(monkeypatch):
    """A real feed with genuine corruption still emits these, just rarely. Killing
    a working encode over that would be worse than the bug being fixed."""
    state = _slate_state()
    _feed(state, SLATE_LINE, 60, 120.0, monkeypatch)  # 0.5/sec

    assert not state.get("slate_kill")
    assert state["proc_obj"].terminated is False


def test_it_needs_sustained_evidence_not_one_burst(monkeypatch):
    """Below SLATE_CONFIRM_SECONDS of evidence it must not act."""
    state = _slate_state()
    _feed(state, SLATE_LINE, 5_000, 5.0, monkeypatch)

    assert not state.get("slate_kill")


def test_the_slate_reads_as_a_capacity_limit_so_the_ladder_cools_down():
    """Rotating cannot help -- every link shares the one account -- so the reason
    string must match is_capacity_limited_error, which cools the link instead."""
    is_capacity_limited_error = RUNNER["is_capacity_limited_error"]
    summary = "upstream connection limit reached: input is a looping slate (350 junk lines/sec), not the live feed"

    assert is_capacity_limited_error(summary)
