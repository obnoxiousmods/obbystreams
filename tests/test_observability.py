"""Covers the diagnostics added after the 2026-08-15 UFC 330 skipping incident.

Each test here pins a behaviour whose absence actively cost diagnosis time that
night: operator events that never reached the journal, a manifest drift nobody
measured, client-reported playback quality that was computed and discarded, and
a restart path that wiped the segment window out from under live viewers.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import (
    SOURCE_QOE,
    _redact_message,
    dash_timeline_drift_seconds,
    record_source_qoe,
    source_switch_allowed,
)

# --- operator events reaching the journal -----------------------------------

def test_event_is_written_to_the_logger(caplog):
    """event() fed a ring buffer and ArangoDB but never the logger, so a restart
    left no record of why it restarted."""
    import app

    with caplog.at_level(logging.INFO, logger="obbystreams"):
        # propagate is disabled in production so uvicorn cannot double-print;
        # caplog needs it on to observe the record.
        app.logger.propagate = True
        try:
            app.event("watchdog restart: managed process exited", "warn")
        finally:
            app.logger.propagate = False

    assert any("watchdog restart" in record.getMessage() for record in caplog.records)
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_event_logging_redacts_provider_credentials():
    """Operator messages embed source URLs whose path is a bearer credential."""
    message = "obbystreams starting -> link [1/3] https://soursignal.com/1012026505/u43qz5tstn/139095"
    redacted = _redact_message(message)

    assert "u43qz5tstn" not in redacted
    assert "1012026505" not in redacted
    # The human-readable part and the identifying tail must survive, or the log
    # line stops being useful for diagnosis.
    assert redacted.startswith("obbystreams starting -> link [1/3]")
    assert "139095" in redacted
    assert "soursignal.com" in redacted


def test_redact_message_leaves_plain_text_untouched():
    assert _redact_message("stream started") == "stream started"


# --- DASH timeline drift ----------------------------------------------------

def _write_mpd(tmp_path, ast, entries, timescale=60000):
    body = "\n".join(entries)
    (tmp_path / "ufc.mpd").write_text(
        f'<MPD availabilityStartTime="{ast}">'
        f'<Representation id="1"><SegmentTemplate timescale="{timescale}">'
        f"<SegmentTimeline>{body}</SegmentTimeline>"
        f"</SegmentTemplate></Representation></MPD>",
        encoding="utf-8",
    )
    return tmp_path


def test_timeline_drift_counts_repeat_and_continuation_entries(tmp_path):
    """<S> continuation entries carry no t=. Requiring it undercounts the
    timeline badly -- that mistake made audio look 28s out of sync."""
    import time

    # 10 segments of 2s each, starting at media time 0, on an AST of "now".
    ast = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time()))
    _write_mpd(tmp_path, ast, ['<S t="0" d="120000" r="4" />', '<S d="120000" r="4" />'])

    drift = dash_timeline_drift_seconds(tmp_path)

    # 10 x 2s of media published against ~0s of elapsed wall clock: the manifest
    # is advertising ~20s of future content.
    assert drift is not None
    assert 18.0 <= drift <= 22.0


def test_timeline_drift_is_none_when_there_is_no_manifest(tmp_path):
    assert dash_timeline_drift_seconds(tmp_path) is None


def test_timeline_drift_is_none_for_an_unparseable_manifest(tmp_path):
    (tmp_path / "ufc.mpd").write_text("<MPD>truncated", encoding="utf-8")
    assert dash_timeline_drift_seconds(tmp_path) is None


# --- client-reported playback quality ---------------------------------------

def test_qoe_records_reattaches_and_latency():
    """A re-attach restarts the playhead at live, so each one IS a visible skip.
    Before this the count had to be reconstructed from nginx access logs."""
    SOURCE_QOE.clear()
    record_source_qoe("src-1", "hash-a", 15.0, buffering_ms=500, stalls=2,
                      reattaches=3, live_latency_seconds=12.5, dropped_frames=7)
    record_source_qoe("src-1", "hash-b", 15.0, buffering_ms=0, stalls=0,
                      reattaches=1, live_latency_seconds=7.5, dropped_frames=0)

    stats = SOURCE_QOE["src-1"]
    assert stats["reattaches"] == 4
    assert stats["stalls"] == 2
    assert stats["dropped_frames"] == 7
    assert stats["latency_samples"] == 2
    assert stats["latency_sum"] == 20.0
    assert len(stats["viewers"]) == 2


def test_qoe_clamps_hostile_client_values():
    """These arrive from the public internet as per-heartbeat deltas."""
    SOURCE_QOE.clear()
    record_source_qoe("src-2", "hash-a", 1.0, buffering_ms=10**9, stalls=-5,
                      reattaches=10**9, live_latency_seconds=10**9, dropped_frames=-1)

    stats = SOURCE_QOE["src-2"]
    assert stats["buffering_ms"] <= 60_000.0
    assert stats["stalls"] == 0
    assert stats["reattaches"] == 1000
    assert stats["dropped_frames"] == 0
    # Out-of-range latency must not pollute the mean.
    assert stats["latency_samples"] == 0


def test_qoe_tolerates_snapshots_written_before_these_fields_existed():
    """The stats file is persisted across restarts and predates these keys."""
    SOURCE_QOE.clear()
    SOURCE_QOE["src-3"] = {"watch_ms": 100.0, "buffering_ms": 0.0, "stalls": 0, "viewers": set()}

    record_source_qoe("src-3", "hash-a", 1.0, buffering_ms=0, stalls=0, reattaches=2)

    assert SOURCE_QOE["src-3"]["reattaches"] == 2


def test_qoe_ignores_a_missing_latency_reading():
    SOURCE_QOE.clear()
    record_source_qoe("src-4", "hash-a", 1.0, buffering_ms=0, stalls=0, live_latency_seconds=None)
    assert SOURCE_QOE["src-4"]["latency_samples"] == 0


# --- switch budget ----------------------------------------------------------

def _switch_config(cooldown=300, max_switches=6):
    return {"private_iptv": {"switch_cooldown_seconds": cooldown, "max_switches_per_card": max_switches}}


def test_force_skips_the_cooldown_but_not_the_budget(monkeypatch):
    """force= is for a segment transition or a confirmed wrong-event feed, which
    must not wait out a 5 minute timer. It must still not restart without limit:
    every switch is visible to every viewer."""
    import app

    monkeypatch.setitem(app.SOURCE_SWITCH_STATE, "switches", 6)
    monkeypatch.setitem(app.SOURCE_SWITCH_STATE, "last_switch_at", app.time.monotonic())

    allowed, reason = source_switch_allowed(_switch_config(), force=True, stream_running=True)

    assert allowed is False
    assert "budget" in reason


def test_force_bypasses_only_the_cooldown(monkeypatch):
    import app

    monkeypatch.setitem(app.SOURCE_SWITCH_STATE, "switches", 0)
    monkeypatch.setitem(app.SOURCE_SWITCH_STATE, "last_switch_at", app.time.monotonic())

    blocked, reason = source_switch_allowed(_switch_config(), force=False, stream_running=True)
    forced, _ = source_switch_allowed(_switch_config(), force=True, stream_running=True)

    assert blocked is False and "cooldown" in reason
    assert forced is True


def test_a_stopped_stream_may_always_switch(monkeypatch):
    import app

    monkeypatch.setitem(app.SOURCE_SWITCH_STATE, "switches", 99)
    allowed, _ = source_switch_allowed(_switch_config(), force=False, stream_running=False)
    assert allowed is True


# --- metrics exposition -----------------------------------------------------

def test_metrics_endpoint_exposes_counters_and_requires_auth(client, anon_client):
    assert anon_client.get("/metrics").status_code == 401

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    for family in (
        "obbystreams_up",
        "obbystreams_stream_restarts_total",
        "obbystreams_watchdog_restarts_total",
        "obbystreams_dash_timeline_drift_seconds",
        "obbystreams_source_reattaches_total",
    ):
        assert f"# TYPE {family}" in body
    assert body.rstrip().endswith("# EOF")


def test_metrics_escapes_label_values(client):
    """Source labels are provider-supplied and reach the exposition as labels."""
    SOURCE_QOE.clear()
    SOURCE_QOE['evil"id'] = {
        "watch_ms": 1000.0, "buffering_ms": 0.0, "stalls": 0, "viewers": set(),
        "reattaches": 1, "dropped_frames": 0, "latency_sum": 0.0, "latency_samples": 0,
        "label": 'PPV "MAIN" \\ EVENT',
    }
    try:
        body = client.get("/metrics").text
    finally:
        SOURCE_QOE.clear()

    assert '\\"MAIN\\"' in body
    assert "\\\\" in body


# --- watchdog must not kill a supervisor that is relaunching ffmpeg ----------

def _stream_cfg(**overrides):
    cfg = {
        "startup_grace_seconds": 25, "min_assessment_seconds": 15,
        "playlist_stale_seconds": 25, "failure_ramp_seconds": 60,
        "success_score_threshold": 180, "failure_score_threshold": -120,
        "confirmed_failure_samples": 2, "health_sample_interval": 0,
    }
    cfg.update(overrides)
    return {"stream": cfg}


def _dead_hls():
    """What the world looks like mid-relaunch: no playlist, nothing progressing."""
    return {"playlist_exists": False, "playlist_ready": False, "playlist_age": None,
            "segments": 0, "bytes": 0, "encode_rate": None, "live_lag_seconds": None}


def test_watchdog_does_not_confirm_failure_while_ffmpeg_is_relaunching():
    """The supervisor restarts ffmpeg itself on an upstream blip. For those
    seconds there is no encoder and no playlist, which scores far below the
    failure threshold -- and confirming it kills the recovery in progress."""
    import app

    scorer = app.StreamHealthScorer()
    long_lived = {"managed": True, "pid": 1, "age": 1800.0, "encoder_age": 900.0, "children": [{"name": "ffmpeg"}]}
    healthy_hls = {"playlist_exists": True, "playlist_ready": True, "playlist_age": 1.0,
                   "segments": 15, "bytes": 5_000_000, "encode_rate": 1.0, "live_lag_seconds": 2.0}

    # Established, healthy, well past every grace window.
    scorer.assess(_stream_cfg(), long_lived, healthy_hls, force=True)

    # ffmpeg exits; the supervisor is still up and about to relaunch it.
    mid_relaunch = {"managed": True, "pid": 1, "age": 1802.0, "encoder_age": None, "children": []}
    first = scorer.assess(_stream_cfg(), mid_relaunch, _dead_hls(), force=True)
    second = scorer.assess(_stream_cfg(), mid_relaunch, _dead_hls(), force=True)

    assert first["decision"] != "failed"
    assert second["decision"] != "failed", "two bad samples must not confirm failure mid-relaunch"


def test_a_supervisor_with_no_encoder_for_too_long_is_still_failed():
    """The grace must not become a blanket amnesty: a wedged supervisor that never
    brings ffmpeg back is exactly what the watchdog exists to catch."""
    import app

    scorer = app.StreamHealthScorer()
    long_lived = {"managed": True, "pid": 1, "age": 1800.0, "encoder_age": 900.0, "children": [{"name": "ffmpeg"}]}
    healthy_hls = {"playlist_exists": True, "playlist_ready": True, "playlist_age": 1.0,
                   "segments": 15, "bytes": 5_000_000, "encode_rate": 1.0, "live_lag_seconds": 2.0}
    scorer.assess(_stream_cfg(), long_lived, healthy_hls, force=True)

    # Push the last-encoder sighting well outside the grace window.
    scorer.last_encoder_seen_at = app.time.monotonic() - 600
    wedged = {"managed": True, "pid": 1, "age": 2400.0, "encoder_age": None, "children": []}

    scorer.assess(_stream_cfg(), wedged, _dead_hls(), force=True)
    final = scorer.assess(_stream_cfg(), wedged, _dead_hls(), force=True)

    assert final["decision"] == "failed"


# --- slow-upstream alerting -------------------------------------------------

def test_a_slow_upstream_alerts_only_once_it_is_sustained(monkeypatch):
    """The health score stays healthy while a degrading feed delivers at 0.7x --
    frames flow and the playlist stays fresh -- so nothing else notices."""
    import app

    sent = []
    monkeypatch.setattr(app, "SCHEDULER", None)          # no Discord in tests
    monkeypatch.setattr(app, "_LOW_ENCODE_RATE_SINCE", None)
    monkeypatch.setattr(app, "_LAST_LOW_RATE_ALERT_AT", None)
    monkeypatch.setattr(app, "event", lambda msg, level="info", extra=None: sent.append(msg))

    clock = {"t": 1000.0}
    monkeypatch.setattr(app.time, "monotonic", lambda: clock["t"])

    asyncio.run(app.maybe_alert_slow_upstream({"encode_rate": 0.75}))
    assert sent == [], "must not fire on the first low sample"

    clock["t"] += app.ENCODE_RATE_SUSTAIN_SECONDS - 1
    asyncio.run(app.maybe_alert_slow_upstream({"encode_rate": 0.75}))
    assert sent == [], "must not fire before the sustain window elapses"

    clock["t"] += 5
    asyncio.run(app.maybe_alert_slow_upstream({"encode_rate": 0.75}))
    assert len(sent) == 1
    assert "0.75x" in sent[0]


def test_recovering_to_realtime_clears_the_slow_upstream_state(monkeypatch):
    import app

    sent = []
    monkeypatch.setattr(app, "SCHEDULER", None)
    monkeypatch.setattr(app, "_LOW_ENCODE_RATE_SINCE", None)
    monkeypatch.setattr(app, "_LAST_LOW_RATE_ALERT_AT", None)
    monkeypatch.setattr(app, "event", lambda msg, level="info", extra=None: sent.append(msg))

    clock = {"t": 1000.0}
    monkeypatch.setattr(app.time, "monotonic", lambda: clock["t"])

    asyncio.run(app.maybe_alert_slow_upstream({"encode_rate": 0.75}))
    clock["t"] += app.ENCODE_RATE_SUSTAIN_SECONDS + 5
    # A healthy sample in between must reset the clock, not carry the elapsed time.
    asyncio.run(app.maybe_alert_slow_upstream({"encode_rate": 1.0}))
    asyncio.run(app.maybe_alert_slow_upstream({"encode_rate": 0.75}))

    assert sent == []


def test_an_unknown_encode_rate_never_alerts(monkeypatch):
    """encode_rate is None before the first samples land."""
    import app

    sent = []
    monkeypatch.setattr(app, "SCHEDULER", None)
    monkeypatch.setattr(app, "_LOW_ENCODE_RATE_SINCE", None)
    monkeypatch.setattr(app, "_LAST_LOW_RATE_ALERT_AT", None)
    monkeypatch.setattr(app, "event", lambda msg, level="info", extra=None: sent.append(msg))

    asyncio.run(app.maybe_alert_slow_upstream({"encode_rate": None}))
    assert sent == []
