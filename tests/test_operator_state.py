"""Tests for the persistent operator Stop/Start master kill switch.

Covers normalization/persistence of ``stream.operator_stopped``, the watchdog
gate, and the invariant that ``start_managed_process`` never mutates operator
intent as a side effect.
"""

from app import (
    load_config,
    normalize_config,
    operator_stopped,
    set_operator_stopped,
    should_watchdog_restart_exited_process,
)


def test_operator_stopped_defaults_false():
    cfg = normalize_config({})
    assert cfg["stream"]["operator_stopped"] is False
    assert operator_stopped(cfg) is False


def test_operator_stopped_normalized_to_bool():
    cfg = normalize_config({"stream": {"operator_stopped": 1}})
    assert cfg["stream"]["operator_stopped"] is True
    assert operator_stopped(cfg) is True


def test_set_operator_stopped_persists_through_save_and_load(config_path):
    cfg = load_config(fresh=True)
    assert operator_stopped(cfg) is False
    set_operator_stopped(cfg, True)
    # A completely fresh read from disk must see the persisted stop.
    reloaded = load_config(fresh=True)
    assert operator_stopped(reloaded) is True
    set_operator_stopped(reloaded, False)
    assert operator_stopped(load_config(fresh=True)) is False


def test_reconcile_preserves_a_stop_persisted_after_snapshot(config_path):
    # Simulates the private-IPTV race: a long-lived config snapshot thinks the
    # stream is running, but an operator Stop was persisted meanwhile. Reconcile
    # must stamp the persisted stop back in before that snapshot is saved.
    import app

    app.set_operator_stopped(app.load_config(fresh=True), True)  # operator stops
    stale_snapshot = app.load_config(fresh=True)
    stale_snapshot["stream"]["operator_stopped"] = False  # snapshot is now stale
    app._reconcile_operator_stopped(stale_snapshot)
    assert stale_snapshot["stream"]["operator_stopped"] is True


def test_watchdog_gate_blocks_restart_when_operator_stopped():
    stopped = normalize_config(
        {"stream": {"operator_stopped": True, "links": ["https://a.example.com/live.m3u8"]}}
    )
    running = normalize_config(
        {"stream": {"operator_stopped": False, "links": ["https://a.example.com/live.m3u8"]}}
    )
    assert should_watchdog_restart_exited_process(stopped, "running") is False
    assert should_watchdog_restart_exited_process(running, "running") is True


def test_watchdog_gate_still_respects_desired_state_and_links():
    running = normalize_config(
        {"stream": {"operator_stopped": False, "links": ["https://a.example.com/live.m3u8"]}}
    )
    # desired_state stopped -> no restart even if operator flag is clear.
    assert should_watchdog_restart_exited_process(running, "stopped") is False
    # no links -> nothing to restart.
    no_links = normalize_config({"stream": {"operator_stopped": False, "links": []}})
    assert should_watchdog_restart_exited_process(no_links, "running") is False


def test_start_managed_process_does_not_touch_operator_state(monkeypatch):
    import app

    # The desired-state global must be owned by the endpoints only; a start
    # triggered by the watchdog/scraper must not silently re-arm it.
    monkeypatch.setattr(app, "STREAM_DESIRED_STATE", "stopped", raising=False)

    class _FakeProc:
        pid = 4321

        def poll(self):
            return None

    monkeypatch.setattr(app.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(app, "kill_existing_streams", list)
    monkeypatch.setattr(app, "build_command", lambda config, links: ["true"])
    monkeypatch.setattr(app, "read_process_output", lambda proc: None)
    monkeypatch.setattr(app.asyncio, "create_task", lambda *a, **k: None)
    app.STREAM_HEALTH_SCORER.reset()

    cfg = normalize_config({"stream": {"links": ["https://a.example.com/live.m3u8"]}})
    pid, _cmd = app.start_managed_process(cfg, ["https://a.example.com/live.m3u8"], kill_existing=False)
    assert pid == 4321
    assert app.STREAM_DESIRED_STATE == "stopped"
    # cleanup module globals we touched
    app.PROCESS = None
    app.STARTED_AT = None


# --- Auto-schedule interplay with the operator Stop ---------------------------


def test_set_operator_stopped_records_and_clears_the_reason():
    import app

    cfg = normalize_config({})
    app.set_operator_stopped(cfg, True, "schedule")
    assert cfg["stream"]["operator_stopped"] is True
    assert cfg["stream"]["stop_reason"] == "schedule"

    # Starting always clears the reason, whoever set it.
    app.set_operator_stopped(cfg, False)
    assert cfg["stream"]["stop_reason"] == ""


def test_stop_reason_only_reports_while_stopped():
    import app

    stopped = normalize_config({"stream": {"operator_stopped": True, "stop_reason": "schedule"}})
    running = normalize_config({"stream": {"operator_stopped": False, "stop_reason": "schedule"}})

    assert app.stop_reason(stopped) == "schedule"
    assert app.stop_reason(running) == ""


def test_normalize_rejects_an_unknown_stop_reason():
    cfg = normalize_config({"stream": {"operator_stopped": True, "stop_reason": "gremlins"}})
    assert cfg["stream"]["stop_reason"] == ""


def test_watchdog_still_idles_during_a_scheduled_standby():
    """A scheduler stand-down is an operator Stop, so auto-recovery stays off."""
    cfg = normalize_config(
        {
            "stream": {
                "operator_stopped": True,
                "stop_reason": "schedule",
                "links": ["https://a.example.com/live.m3u8"],
            }
        }
    )
    assert should_watchdog_restart_exited_process(cfg, "running") is False


def test_schedule_section_round_trips_through_normalize():
    cfg = normalize_config({"schedule": {"enabled": False, "lead_minutes": 45}})
    assert cfg["schedule"]["enabled"] is False
    assert cfg["schedule"]["lead_minutes"] == 45
    # Re-normalizing (what save_config does) must not lose it.
    assert normalize_config(cfg)["schedule"]["lead_minutes"] == 45


def test_schedule_settings_are_bounded():
    cfg = normalize_config({"schedule": {"lead_minutes": 99999, "live_poll_seconds": 1}})
    assert cfg["schedule"]["lead_minutes"] == 720
    assert cfg["schedule"]["live_poll_seconds"] == 30
