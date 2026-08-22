"""Per-run segment namespacing and the encoder flags that shape the timeline.

Relaunching ffmpeg used to wipe the whole output directory first, so a ~2s
process blip pulled the init segments and the entire DVR window out from under
every connected player at once. These pin the replacement behaviour.
"""

import argparse
import os
import runpy
import time
from pathlib import Path
from typing import ClassVar

RUNNER = runpy.run_path(str(Path(__file__).resolve().parents[1] / "bin" / "obbystreams"))

prune_foreign_runs = RUNNER["prune_foreign_runs"]
run_token = RUNNER["run_token"]
build_ffmpeg_cmd = RUNNER["build_ffmpeg_cmd"]
cleanup = RUNNER["cleanup"]


def _touch(directory, *names, age_seconds=0):
    for name in names:
        path = directory / name
        path.write_text("x", encoding="utf-8")
        if age_seconds:
            stamp = time.time() - age_seconds
            os.utime(path, (stamp, stamp))


# --- run namespacing --------------------------------------------------------

def test_prune_drops_old_foreign_runs_and_keeps_the_named_ones(tmp_path):
    _touch(
        tmp_path,
        "ufc_raaa_chunk_1_000001.m4s", "ufc_raaa_init_1.m4s",
        age_seconds=600,
    )
    _touch(
        tmp_path,
        "ufc_rbbb_chunk_1_000001.m4s", "ufc_rbbb_init_1.m4s", "ufc_rccc_chunk_1_000001.m4s",
        age_seconds=600,
    )

    removed = prune_foreign_runs(tmp_path, {"bbb", "ccc"})

    assert removed == 2
    survivors = sorted(p.name for p in tmp_path.iterdir())
    assert survivors == [
        "ufc_rbbb_chunk_1_000001.m4s", "ufc_rbbb_init_1.m4s", "ufc_rccc_chunk_1_000001.m4s",
    ]


def test_a_foreign_run_survives_the_handover_window(tmp_path):
    """The common restart path is a whole new supervisor, which knows nothing of
    the outgoing run's token. Deleting its segments on sight would 404 every
    viewer mid-handover -- exactly the failure this replaced."""
    _touch(tmp_path, "ufc_rold_chunk_1_000001.m4s")

    prune_foreign_runs(tmp_path, set())

    assert (tmp_path / "ufc_rold_chunk_1_000001.m4s").exists()

    # ...and it does not linger forever once nobody can still be fetching it.
    _touch(tmp_path, "ufc_rold_chunk_1_000002.m4s", age_seconds=600)
    prune_foreign_runs(tmp_path, set())
    assert not (tmp_path / "ufc_rold_chunk_1_000002.m4s").exists()


def test_prune_never_touches_the_manifests(tmp_path):
    """Leaving the outgoing manifest in place is the point: until the incoming
    run rewrites it, players keep serving themselves from segments that exist."""
    _touch(tmp_path, "ufc.mpd", "ufc.m3u8", "media_0.m3u8")
    _touch(tmp_path, "ufc_rold_chunk_1_000001.m4s", age_seconds=600)

    prune_foreign_runs(tmp_path, {"new"})

    assert (tmp_path / "ufc.mpd").exists()
    assert (tmp_path / "ufc.m3u8").exists()
    assert (tmp_path / "media_0.m3u8").exists()
    assert not (tmp_path / "ufc_rold_chunk_1_000001.m4s").exists()


def test_prune_ignores_unrelated_files(tmp_path):
    _touch(tmp_path, "notes.txt", ".encode-progress.json", "ufc_rzzz_chunk_1_000001.m4s")

    prune_foreign_runs(tmp_path, {"zzz"})

    assert (tmp_path / "notes.txt").exists()
    assert (tmp_path / ".encode-progress.json").exists()


def test_prune_on_an_empty_directory_is_a_no_op(tmp_path):
    assert prune_foreign_runs(tmp_path, {"any"}) == 0


def test_cleanup_still_removes_namespaced_segments(tmp_path):
    """cleanup() remains the cold-start/shutdown wipe and must not leak runs."""
    _touch(tmp_path, "ufc_raaa_chunk_1_000001.m4s", "ufc_raaa_init_1.m4s", "ufc.mpd", "media_0.m3u8")

    cleanup(str(tmp_path))

    assert list(tmp_path.iterdir()) == []


def test_run_token_is_filename_safe_and_sortable():
    token = run_token()
    assert token.isalnum()
    assert token == token.lower()
    # Hex seconds: lexical order matches chronological order at fixed width.
    assert len(run_token()) == len(token)


# --- encoder flags ----------------------------------------------------------

def _args(**overrides):
    defaults = {
        "verbose": False, "reconnect": True, "reconnect_delay": 5, "timeout": 10,
        "thread_queue_size": 8192, "analyzeduration": 2_000_000, "probesize": 2_000_000,
        "source_headers": {}, "preset": "veryfast",
        "video_bitrate_720": "3500k", "maxrate_720": "4500k", "bufsize_720": "9000k",
        "video_bitrate_1080": "6M", "maxrate_1080": "7.5M", "bufsize_1080": "9M",
        "audio_bitrate": "192k", "gop": 120, "hls_time": 2, "hls_size": 15,
        "hls_delete_threshold": 20, "output_dir": "/tmp/out",
        "utc_timing_url": "https://example.com/time",
        "readrate": 1.0, "readrate_initial_burst": 4.0, "readrate_catchup": 2.0,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class _Encoder:
    key = "nvenc"
    global_args: ClassVar[list[str]] = []


def _flag_value(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_segment_names_carry_the_run_id():
    cmd = build_ffmpeg_cmd(_args(), "https://example.com/live.ts", _Encoder(), run_id="deadbeef")

    assert "ufc_rdeadbeef_init_" in _flag_value(cmd, "-init_seg_name")
    assert "ufc_rdeadbeef_chunk_" in _flag_value(cmd, "-media_seg_name")


def test_input_is_paced_with_a_bounded_burst_and_a_usable_catchup():
    """Unpaced, the connect-time backlog is drained at ~1.8x and the media clock
    ends up permanently ahead of availabilityStartTime. Paced at 1x with ffmpeg's
    default 1.05 catchup, real lag never drains and compounds instead."""
    cmd = build_ffmpeg_cmd(_args(), "https://example.com/live.ts", _Encoder())

    assert _flag_value(cmd, "-readrate") == "1.0"
    assert float(_flag_value(cmd, "-readrate_initial_burst")) > 0
    assert float(_flag_value(cmd, "-readrate_catchup")) > 1.05
    # -re is -readrate 1 by another name; it must not also be present.
    assert "-re" not in cmd


def test_pacing_can_be_disabled():
    cmd = build_ffmpeg_cmd(_args(readrate=0), "https://example.com/live.ts", _Encoder())
    assert "-readrate" not in cmd


def test_local_input_is_never_paced():
    cmd = build_ffmpeg_cmd(_args(), "/media/local.ts", _Encoder())
    assert "-readrate" not in cmd


def test_keyint_min_is_not_pinned_to_the_gop():
    """keyint_min == g forbids closing a GOP early, which is exactly what
    force_key_frames asks for; the two disagree and segments come out uneven."""
    cmd = build_ffmpeg_cmd(_args(), "https://example.com/live.ts", _Encoder())

    assert not any(flag.startswith("-keyint_min") for flag in cmd)
    assert "expr:gte(t,n_forced*2)" in _flag_value(cmd, "-force_key_frames:v:0")


def test_audio_drift_is_corrected_continuously():
    """async=1 aligns audio once at t=0 and then lets it free-run, so a feed that
    drifts desyncs progressively across a multi-hour card."""
    cmd = build_ffmpeg_cmd(_args(), "https://example.com/live.ts", _Encoder())

    audio_filter = _flag_value(cmd, "-af")
    assert "async=1000" in audio_filter
    assert "async=1:" not in audio_filter


def test_utc_timing_is_published_for_dynamic_manifests():
    """Without <UTCTiming> a dynamic MPD makes every player locate the live edge
    with its own device clock."""
    cmd = build_ffmpeg_cmd(_args(), "https://example.com/live.ts", _Encoder())
    assert _flag_value(cmd, "-utc_timing_url") == "https://example.com/time"


def test_utc_timing_is_omitted_when_unset():
    cmd = build_ffmpeg_cmd(_args(utc_timing_url=""), "https://example.com/live.ts", _Encoder())
    assert "-utc_timing_url" not in cmd


def test_retention_exceeds_the_published_window():
    """A viewer drifting behind the live edge must still find their segment on
    disk; when it has already been deleted they 404 and the player re-attaches."""
    args = _args()
    cmd = build_ffmpeg_cmd(args, "https://example.com/live.ts", _Encoder())

    assert int(_flag_value(cmd, "-extra_window_size")) > int(_flag_value(cmd, "-window_size")) // 2


def test_legacy_segments_are_swept_once_they_are_old(tmp_path):
    """Segments written before per-run naming carry no token, so the run-based
    sweep cannot see them and they would leak on disk forever after the upgrade."""



    _touch(tmp_path, "ufc_chunk_1_000001.m4s", "ufc_init_1.m4s")
    old = time.time() - 600
    for name in ("ufc_chunk_1_000001.m4s", "ufc_init_1.m4s"):
        os.utime(tmp_path / name, (old, old))

    prune_foreign_runs(tmp_path, {"new"})

    assert not (tmp_path / "ufc_chunk_1_000001.m4s").exists()
    assert not (tmp_path / "ufc_init_1.m4s").exists()


def test_recent_legacy_segments_survive_the_handover(tmp_path):
    """On the first namespaced run these are the outgoing run's segments, still
    being fetched by live viewers."""
    _touch(tmp_path, "ufc_chunk_1_000001.m4s")

    prune_foreign_runs(tmp_path, {"new"})

    assert (tmp_path / "ufc_chunk_1_000001.m4s").exists()


# --- slow-upstream auto-rotation --------------------------------------------

parse_out_time = RUNNER["parse_out_time"]
next_available_index = RUNNER["next_available_index"]
StreamHealth = RUNNER["StreamHealth"]


def test_parse_out_time_reads_ffmpeg_progress():
    assert parse_out_time("00:22:21.139800") == 1341.1398
    assert parse_out_time("01:00:00.000000") == 3600.0
    # Absent or malformed must not be mistaken for zero progress.
    assert parse_out_time(None) is None
    assert parse_out_time("N/A") is None
    assert parse_out_time("") is None


def test_rotation_skips_links_that_are_backed_off():
    import time as _t

    health = [StreamHealth(i, f"https://example.com/{i}") for i in range(4)]
    now = _t.monotonic()
    health[1].backoff_until = now + 900   # just rotated away from, still slow
    health[2].backoff_until = now + 900

    assert next_available_index(health, 0) == 3


def test_rotation_stays_put_when_every_alternative_is_backed_off():
    """Rotating in place would restart onto the same slow feed for nothing."""
    import time as _t

    health = [StreamHealth(i, f"https://example.com/{i}") for i in range(3)]
    now = _t.monotonic()
    for h in health[1:]:
        h.backoff_until = now + 900

    assert next_available_index(health, 0) == 0


def test_rotation_wraps_around():
    health = [StreamHealth(i, f"https://example.com/{i}") for i in range(3)]
    assert next_available_index(health, 2) == 0


def test_a_single_link_never_rotates():
    health = [StreamHealth(0, "https://example.com/only")]
    assert next_available_index(health, 0) == 0


def _drift_decision(samples, max_drift=60.0):
    """Replay (wall_seconds, content_seconds) samples through the watchdog's rule.

    Mirrors realtime_watchdog_thread: rotate only once accumulated drift exceeds
    the ceiling AND the stream is not currently catching up.
    """
    base_at, base_out = samples[0]
    prev_at, prev_out = samples[0]
    for at, out in samples[1:]:
        rate = (out - prev_out) / (at - prev_at)
        prev_at, prev_out = at, out
        deficit = (at - base_at) - (out - base_out)
        if deficit >= max_drift and rate < 1.0:
            return True, deficit
    return False, 0.0


def test_a_recoverable_dip_does_not_rotate():
    """The real 2026-08-15 19:28 dip: ~2 minutes at 0.55-0.84x, then readrate
    catchup drained 37s of deficit back to zero on its own. Rotating on a run of
    slow samples fires here and costs viewers a switch for nothing."""
    samples = [(0.0, 0.0)]
    for rate in (1.0, 1.0, 0.84, 0.55, 0.60, 0.67, 1.27, 1.25, 1.22, 1.30, 1.30, 1.20):
        at, out = samples[-1]
        samples.append((at + 30.0, out + 30.0 * rate))

    rotated, _ = _drift_decision(samples)
    assert rotated is False


def test_a_feed_that_keeps_losing_ground_rotates():
    """The 139097 failure: sustained 0.73-0.80x, drift growing monotonically past
    90s with nothing draining it."""
    samples = [(0.0, 0.0)]
    for rate in (0.98, 0.90, 0.82, 0.88, 0.76, 0.73, 0.75, 0.74, 0.76, 0.73, 0.72, 0.74):
        at, out = samples[-1]
        samples.append((at + 30.0, out + 30.0 * rate))

    rotated, deficit = _drift_decision(samples)
    assert rotated is True
    assert deficit >= 60.0


def test_a_stream_far_behind_but_catching_up_is_left_alone():
    """Deficit alone is not enough: while catchup is draining it, a rotation
    would throw away the recovery already in progress."""
    # Fall a long way behind at a steady 0.7x...
    samples = [(0.0, 0.0)]
    for _ in range(10):
        at, out = samples[-1]
        samples.append((at + 30.0, out + 30.0 * 0.7))
    behind = (samples[-1][0]) - (samples[-1][1])
    assert behind > 60.0, "precondition: deeply behind"

    # ...then evaluate one interval where catchup is actively draining it.
    at, out = samples[-1]
    catching_up = [*samples[:-1], (at, out), (at + 30.0, out + 30.0 * 1.5)]
    rate = 1.5
    deficit = (catching_up[-1][0] - catching_up[0][0]) - (catching_up[-1][1] - catching_up[0][1])

    assert deficit >= 60.0, "still far behind"
    assert not (deficit >= 60.0 and rate < 1.0), "must not rotate while catching up"
