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
