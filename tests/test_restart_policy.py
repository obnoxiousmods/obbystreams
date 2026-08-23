"""When a restart is worth interrupting viewers, and which link it resumes on.

Every encode restart costs every connected viewer a re-attach: segment numbering
restarts, availabilityStartTime is re-stamped, the init segment changes, and
nothing carries an EXT-X-DISCONTINUITY across a process boundary. So the two
questions that matter are "did this restart need to happen?" and "did it resume
on the feed that was already working?".

Both were answered wrongly on 2026-08-22:

  14:10:52  a source refresh killed a 19-minute-old encode running at 1.00x with
            0 dropped frames, because an unrelated link had entered the pool. The
            link it had been ingesting was still there, at position 3. The
            restart re-entered at link 1 -- which had failed 20 minutes earlier.

  14:45:18  the watchdog fired on a stale playlist and restarted onto the very
            link that had just gone stale, failed again, then walked links 2 and
            3 before settling on 4.
"""

import json
import time
from typing import ClassVar

import pytest

import app


def _write_progress(tmp_path, url, *, age_seconds=0.0):
    (tmp_path / ".encode-progress.json").write_text(
        json.dumps({"speed": "1.01x", "at": time.time() - age_seconds, "link_url": url}),
        encoding="utf-8",
    )
    return {"stream": {"output_dir": str(tmp_path)}}


class TestActiveEncodeLink:
    def test_reads_the_link_the_wrapper_published(self, tmp_path):
        config = _write_progress(tmp_path, "https://provider/live/139094")
        assert app.active_encode_link(config) == "https://provider/live/139094"

    def test_a_stale_file_means_nothing_is_running(self, tmp_path):
        # The wrapper rewrites this on every ffmpeg stats block (~1s). Anything
        # older than the window is a leftover from a dead encode, and trusting it
        # would pin a restart to a link that is not actually live.
        config = _write_progress(tmp_path, "https://provider/live/139094", age_seconds=60.0)
        assert app.active_encode_link(config) == ""

    def test_missing_or_unconfigured_is_not_an_error(self, tmp_path):
        assert app.active_encode_link({"stream": {"output_dir": str(tmp_path)}}) == ""
        assert app.active_encode_link({"stream": {}}) == ""
        assert app.active_encode_link({}) == ""

    def test_a_corrupt_file_is_not_an_error(self, tmp_path):
        (tmp_path / ".encode-progress.json").write_text("{not json", encoding="utf-8")
        assert app.active_encode_link({"stream": {"output_dir": str(tmp_path)}}) == ""


class TestLinkOrdering:
    LINKS: ClassVar[list[str]] = ["https://p/1", "https://p/2", "https://p/3", "https://p/4"]

    def test_a_restart_resumes_on_the_link_that_was_working(self):
        # The 14:10 case: ffmpeg was on link 4 and healthy.
        assert app.links_with_active_first(self.LINKS, "https://p/4") == [
            "https://p/4",
            "https://p/1",
            "https://p/2",
            "https://p/3",
        ]

    def test_a_watchdog_restart_tries_the_failed_link_last(self):
        # The 14:45 case: link 1 had just gone stale.
        assert app.links_with_active_last(self.LINKS, "https://p/1") == [
            "https://p/2",
            "https://p/3",
            "https://p/4",
            "https://p/1",
        ]

    def test_a_link_that_left_the_pool_does_not_reorder_anything(self):
        assert app.links_with_active_first(self.LINKS, "https://p/gone") == self.LINKS
        assert app.links_with_active_last(self.LINKS, "https://p/gone") == self.LINKS

    def test_an_unknown_active_link_is_a_no_op(self):
        assert app.links_with_active_first(self.LINKS, "") == self.LINKS
        assert app.links_with_active_last(self.LINKS, "") == self.LINKS

    def test_a_single_link_is_never_reordered(self):
        assert app.links_with_active_last(["https://p/1"], "https://p/1") == ["https://p/1"]
        assert app.links_with_active_first(["https://p/1"], "https://p/1") == ["https://p/1"]

    def test_ordering_never_drops_or_duplicates_a_link(self):
        for reorder in (app.links_with_active_first, app.links_with_active_last):
            result = reorder(self.LINKS, "https://p/3")
            assert sorted(result) == sorted(self.LINKS)
            assert len(result) == len(self.LINKS)


class TestQoeDiagnostics:
    """last_error / mirror_id ingestion. These arrive from the public internet."""

    def setup_method(self):
        app.SOURCE_QOE.clear()

    def teardown_method(self):
        app.SOURCE_QOE.clear()

    def _record(self, **kwargs):
        app.record_source_qoe("server-1", "hash", 15.0, 0, 0, **kwargs)

    def test_tallies_errors_and_mirrors(self):
        self._record(last_error="Playback stalled at the live edge.", mirror_id="fight")
        self._record(last_error="Playback stalled at the live edge.", mirror_id="live")
        self._record(last_error="Stream restarted at the source.", mirror_id="fight")

        stats = app.SOURCE_QOE["server-1"]
        assert stats["errors"]["Playback stalled at the live edge."] == 2
        assert stats["errors"]["Stream restarted at the source."] == 1
        assert stats["mirrors"] == {"fight": 2, "live": 1}

    def test_caps_distinct_keys_so_a_hostile_client_cannot_grow_it(self):
        for index in range(200):
            self._record(last_error=f"error-{index}", mirror_id=f"mirror-{index}")
        stats = app.SOURCE_QOE["server-1"]
        assert len(stats["errors"]) == 40
        assert len(stats["mirrors"]) == 20

    def test_truncates_long_values(self):
        self._record(last_error="x" * 5000, mirror_id="y" * 5000)
        stats = app.SOURCE_QOE["server-1"]
        assert max(len(k) for k in stats["errors"]) == 120
        assert max(len(k) for k in stats["mirrors"]) == 40

    def test_absent_fields_are_not_recorded(self):
        self._record()
        stats = app.SOURCE_QOE["server-1"]
        assert stats["errors"] == {}
        assert stats["mirrors"] == {}

    def test_a_snapshot_predating_these_keys_still_loads(self):
        # Persisted QoE from before this change has neither key.
        app.SOURCE_QOE["server-1"] = {
            "watch_ms": 1.0, "buffering_ms": 0.0, "stalls": 0, "viewers": set(),
        }
        self._record(last_error="boom", mirror_id="fight")
        assert app.SOURCE_QOE["server-1"]["errors"] == {"boom": 1}


class TestPlaybackDiagnostics:
    """The ~24 player metrics and the event timeline behind them.

    These exist because the 2026-08-22 freeze -- nginx serving a playlist up to
    30s stale -- was invisible to every server-side check. The origin was correct
    the whole time; only a client could see the manifest had stopped advancing.
    Everything here arrives from a public, unauthenticated endpoint.
    """

    def setup_method(self):
        app.SOURCE_QOE.clear()

    def teardown_method(self):
        app.SOURCE_QOE.clear()

    def _record(self, playback=None, events=None):
        app.record_source_qoe("server-1", "hash", 15.0, 0, 0, playback=playback, events=events)

    def test_counters_accumulate_across_heartbeats(self):
        for _ in range(3):
            self._record({"stall_events": 2, "gap_jumps": 1, "manifest_sequence_regressions": 4})
        stats = app.SOURCE_QOE["server-1"]
        assert stats["stall_events"] == 6
        assert stats["gap_jumps"] == 3
        assert stats["manifest_sequence_regressions"] == 12

    def test_gauges_average_only_over_beats_that_reported_them(self):
        self._record({"buffer_min_seconds": 10.0})
        self._record({"buffer_min_seconds": 4.0})
        self._record({})  # reported nothing -- must not count as a zero
        stats = app.SOURCE_QOE["server-1"]
        assert stats["buffer_min_seconds_samples"] == 2
        assert stats["buffer_min_seconds_sum"] == 14.0

    def test_a_null_gauge_is_not_a_zero(self):
        # "nobody measured it" and "it measured zero" are different answers, and
        # conflating them silently drags every average toward 0.
        self._record({"manifest_advance_rate": None})
        assert app.SOURCE_QOE["server-1"]["manifest_advance_rate_samples"] == 0

    def test_clamps_hostile_counter_values(self):
        self._record({"stall_events": 10**9, "gap_jumps": -50})
        stats = app.SOURCE_QOE["server-1"]
        assert stats["stall_events"] == 1_000      # capped
        assert stats["gap_jumps"] == 0             # negatives floor at 0

    def test_rejects_non_finite_and_out_of_range_gauges(self):
        self._record({"buffer_min_seconds": float("inf")})
        self._record({"buffer_min_seconds": float("nan")})
        self._record({"live_latency_max_seconds": 10**9})
        stats = app.SOURCE_QOE["server-1"]
        assert stats["buffer_min_seconds_samples"] == 0
        assert stats["live_latency_max_seconds_samples"] == 0

    def test_ignores_garbage_types(self):
        self._record({"stall_events": "banana", "buffer_min_seconds": {"a": 1}})
        stats = app.SOURCE_QOE["server-1"]
        assert stats["stall_events"] == 0
        assert stats["buffer_min_seconds_samples"] == 0

    def test_tallies_the_event_timeline_by_kind(self):
        self._record(events=[{"t": 1, "kind": "waiting"}, {"t": 2, "kind": "waiting"},
                             {"t": 3, "kind": "shaka-gapjump"}])
        assert app.SOURCE_QOE["server-1"]["event_kinds"] == {"waiting": 2, "shaka-gapjump": 1}

    def test_bounds_the_event_timeline(self):
        self._record(events=[{"kind": f"k{i}"} for i in range(500)])
        kinds = app.SOURCE_QOE["server-1"]["event_kinds"]
        # Both the per-beat count and the distinct-key count are bounded, or a
        # hostile client grows this dict without limit.
        assert len(kinds) <= app.QOE_EVENT_CARDINALITY
        assert sum(kinds.values()) <= app.QOE_MAX_EVENTS

    def test_survives_malformed_event_entries(self):
        self._record(events=["nope", None, 42, {"no_kind": 1}, {"kind": ""}, {"kind": "ok"}])
        assert app.SOURCE_QOE["server-1"]["event_kinds"] == {"ok": 1}

    def test_truncates_long_event_kinds(self):
        self._record(events=[{"kind": "z" * 500}])
        assert max(len(k) for k in app.SOURCE_QOE["server-1"]["event_kinds"]) == app.QOE_EVENT_KIND_MAX

    def test_a_snapshot_predating_these_fields_still_loads(self):
        # Persisted QoE from before this change has none of the new keys.
        app.SOURCE_QOE["server-1"] = {
            "watch_ms": 1.0, "buffering_ms": 0.0, "stalls": 0, "viewers": set(),
        }
        self._record({"stall_events": 1, "buffer_min_seconds": 5.0})
        stats = app.SOURCE_QOE["server-1"]
        assert stats["stall_events"] == 1
        assert stats["buffer_min_seconds_samples"] == 1

    @pytest.mark.asyncio
    async def test_every_declared_metric_round_trips_to_the_snapshot(self):
        # Guards the three places a new metric has to be registered: the spec, the
        # defaults, and the highscores projection. Adding one and forgetting the
        # projection is a silent no-op.
        self._record(
            {name: 1 for name, _ in app.QOE_COUNTERS}
            | {name: 1.0 for name, _ in app.QOE_GAUGES}
        )
        perf = (await app.viewer_highscores_snapshot())["source_performance"]
        row = next(r for r in perf if r["source_id"] == "server-1")
        for name, _ in app.QOE_COUNTERS:
            assert row[name] == 1, name
        for name, _ in app.QOE_GAUGES:
            assert row[name] == 1.0, name

    def test_persisted_qoe_stays_json_serializable(self):
        import json
        self._record({"stall_events": 1}, events=[{"kind": "waiting"}])
        payload = {
            sid: {**{k: v for k, v in q.items() if k != "viewers"}, "viewers": sorted(q.get("viewers") or ())}
            for sid, q in app.SOURCE_QOE.items()
        }
        json.dumps(payload)  # must not raise


class TestFeedQuality:
    """Choosing between upstream feeds by how smoothly they actually deliver.

    Measured 2026-08-22: one soursignal feed ran at 0.4s of upstream read-lag per
    minute, another at 19.5s -- a ~50x difference. The bad one produced
    viewer-visible 1-3s freezes while every server-side check read perfectly
    healthy, because a bursty feed still keeps ffmpeg at speed=1x with zero
    dropped frames. It publishes segments in clumps, and the clumps starve player
    buffers. Nothing in the health scoring could see it.
    """

    def setup_method(self):
        app.LINK_QUALITY.clear()

    def teardown_method(self):
        app.LINK_QUALITY.clear()

    def _settle(self, url, value, times=5):
        for _ in range(times):
            app.record_link_quality(url, value)

    def test_orders_smoothest_first(self):
        self._settle("https://p/bursty", 19.5)
        self._settle("https://p/smooth", 0.4)
        assert app.links_by_quality(["https://p/bursty", "https://p/smooth"]) == [
            "https://p/smooth",
            "https://p/bursty",
        ]

    def test_an_unmeasured_link_outranks_a_known_bad_one(self):
        # Untried beats known-bad: it might be good, and the bad one demonstrably
        # freezes viewers.
        self._settle("https://p/bursty", 19.5)
        assert app.links_by_quality(["https://p/bursty", "https://p/unknown"]) == [
            "https://p/unknown",
            "https://p/bursty",
        ]

    def test_a_known_good_link_outranks_an_unmeasured_one(self):
        self._settle("https://p/smooth", 0.4)
        assert app.links_by_quality(["https://p/unknown", "https://p/smooth"]) == [
            "https://p/smooth",
            "https://p/unknown",
        ]

    def test_ordering_is_stable_for_ties(self):
        links = ["https://p/a", "https://p/b", "https://p/c"]
        assert app.links_by_quality(links) == links

    def test_ordering_never_drops_or_duplicates_a_link(self):
        self._settle("https://p/b", 19.5)
        links = ["https://p/a", "https://p/b", "https://p/c"]
        assert sorted(app.links_by_quality(links)) == sorted(links)

    def test_one_bad_sample_does_not_condemn_a_good_feed(self):
        self._settle("https://p/smooth", 0.4, times=10)
        app.record_link_quality("https://p/smooth", 30.0)
        # EMA: a single blip moves it, but nowhere near the bad threshold.
        assert app.LINK_QUALITY["https://p/smooth"]["lag_per_min"] < app.LINK_LAG_BAD_PER_MIN

    def test_a_feed_that_degrades_is_believed_before_the_card_ends(self):
        self._settle("https://p/was_good", 0.4, times=5)
        self._settle("https://p/was_good", 25.0, times=6)
        assert app.LINK_QUALITY["https://p/was_good"]["lag_per_min"] > app.LINK_LAG_BAD_PER_MIN

    def test_a_single_sample_is_not_yet_a_verdict(self):
        app.record_link_quality("https://p/one", 0.1)
        # One reading ranks as unmeasured; a lucky first minute is not evidence.
        assert app.link_quality_rank("https://p/one") == app.LINK_LAG_BAD_PER_MIN / 2.0

    def test_ignores_garbage_readings(self):
        for bad in (None, "banana", float("nan"), float("inf"), -5.0):
            app.record_link_quality("https://p/x", bad)
        assert "https://p/x" not in app.LINK_QUALITY

    def test_reads_quality_from_the_wrapper_progress_file(self, tmp_path):
        (tmp_path / ".encode-progress.json").write_text(
            json.dumps({
                "speed": "1.00x", "at": time.time(),
                "link_url": "https://p/live", "read_lag_per_min": 12.5,
            }),
            encoding="utf-8",
        )
        config = {"stream": {"output_dir": str(tmp_path)}}
        assert app.active_encode_link(config) == "https://p/live"
        assert app.LINK_QUALITY["https://p/live"]["lag_per_min"] == 12.5

    def test_a_stale_progress_file_records_nothing(self):
        app.record_link_quality("", 5.0)
        assert app.LINK_QUALITY == {}


class TestActiveViewers:
    """Who is watching right now, as opposed to who has watched the most ever."""

    def setup_method(self):
        app.VIEWER_SESSIONS.clear()
        app.VIEWER_STATS.clear()

    def teardown_method(self):
        app.VIEWER_SESSIONS.clear()
        app.VIEWER_STATS.clear()

    def _session(self, sid, ip_hash, source="server-1", age=0.0):
        app.VIEWER_SESSIONS[sid] = {"source_id": source, "at": time.time() - age, "ip_hash": ip_hash}

    def test_every_viewer_gets_a_codename_even_before_any_stats_exist(self):
        # A viewer on their first heartbeat has no VIEWER_STATS row yet. They must
        # still be nameable, or the list shows blanks for exactly the people who
        # just arrived.
        self._session("s1", "abcdef1234567890")
        rows = app.active_viewers_snapshot()
        assert len(rows) == 1
        assert rows[0]["codename"] == app.codename_for("abcdef1234567890")
        assert rows[0]["codename"].strip()

    def test_codename_is_the_same_whether_derived_or_stored(self):
        ip_hash = "beefcafe00000000"
        app.VIEWER_STATS[ip_hash] = {"codename": app.codename_for(ip_hash), "ip_masked": "1.•.•.4", "total": 90.0}
        self._session("s1", ip_hash)
        assert app.active_viewers_snapshot()[0]["codename"] == app.codename_for(ip_hash)

    def test_two_tabs_are_one_viewer(self):
        # Counting sessions instead of people would make this list disagree with
        # the viewer count rendered beside it.
        self._session("s1", "aaaaaaaaaaaaaaaa")
        self._session("s2", "aaaaaaaaaaaaaaaa")
        rows = app.active_viewers_snapshot()
        assert len(rows) == 1
        assert rows[0]["sessions"] == 2

    def test_departed_viewers_are_pruned(self):
        self._session("here", "1111111111111111")
        self._session("gone", "2222222222222222", age=app.VIEWER_SESSION_TTL + 30)
        codenames = {row["codename"] for row in app.active_viewers_snapshot()}
        assert app.codename_for("1111111111111111") in codenames
        assert app.codename_for("2222222222222222") not in codenames

    def test_never_exposes_a_raw_ip(self):
        ip_hash = "3333333333333333"
        app.VIEWER_STATS[ip_hash] = {
            "codename": "Test Viewer", "ip_masked": "207.•.•.91", "total": 10.0,
        }
        self._session("s1", ip_hash)
        row = app.active_viewers_snapshot()[0]
        assert row["ip_masked"] == "207.•.•.91"
        assert "ip" not in row
        assert "ip_hash" not in row

    def test_a_session_without_an_ip_is_omitted_rather_than_shown_blank(self):
        app.VIEWER_SESSIONS["anon"] = {"source_id": "server-1", "at": time.time(), "ip_hash": None}
        assert app.active_viewers_snapshot() == []

    def test_ranks_by_watch_time_and_reports_idleness(self):
        app.VIEWER_STATS["a" * 16] = {"codename": "Aa", "ip_masked": "x", "total": 10.0}
        app.VIEWER_STATS["b" * 16] = {"codename": "Bb", "ip_masked": "y", "total": 500.0}
        self._session("s1", "a" * 16)
        self._session("s2", "b" * 16, age=12.0)
        rows = app.active_viewers_snapshot()
        assert [r["codename"] for r in rows] == ["Bb", "Aa"]
        assert rows[0]["idle_seconds"] >= 11.0
        assert rows[1]["idle_seconds"] < 2.0

    def test_reports_the_source_from_the_most_recent_session(self):
        self._session("old", "cccccccccccccccc", source="server-1", age=20.0)
        self._session("new", "cccccccccccccccc", source="overlay-hls", age=1.0)
        assert app.active_viewers_snapshot()[0]["source_id"] == "overlay-hls"

    def test_honours_the_limit(self):
        for i in range(80):
            self._session(f"s{i}", f"{i:016x}")
        assert len(app.active_viewers_snapshot(limit=10)) == 10

    @pytest.mark.asyncio
    async def test_appears_in_the_public_highscores_payload(self):
        self._session("s1", "dddddddddddddddd")
        payload = await app.viewer_highscores_snapshot()
        assert payload["active_viewers"][0]["codename"] == app.codename_for("dddddddddddddddd")
