import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import (
    StreamHealthScorer,
    build_command,
    classify_stream_log,
    normalize_config,
    normalize_links,
    parse_nvidia_gpu_csv,
    parse_nvidia_pmon,
    parse_nvidia_process_csv,
    rewrite_playlist,
    safe_hls_path,
    should_watchdog_restart_exited_process,
    valid_stream_url,
)


def test_normalize_links_filters_invalid_and_deduplicates():
    links = normalize_links(
        [
            "https://example.com/live.m3u8",
            "https://example.com/live.m3u8",
            "http://backup.example.com/live.m3u8",
            "ftp://bad.example.com/stream",
            "not-a-url",
            "",
        ]
    )
    assert links == ["https://example.com/live.m3u8", "http://backup.example.com/live.m3u8"]


def test_normalize_config_applies_defaults_and_bounds():
    cfg = normalize_config(
        {
            "stream": {
                "playlist_stale_seconds": 1,
                "watchdog_restart_cooldown": -5,
                "startup_grace_seconds": "2",
                "encoder": "gpu-only",
                "ffmpeg_log_dir": "/tmp/obbystreams-test-logs",
                "min_assessment_seconds": "1",
                "health_sample_interval": "0",
                "confirmed_failure_samples": "0",
                "failure_ramp_seconds": "2",
                "links": ["https://ok.example.com/a.m3u8", "https://ok.example.com/a.m3u8", "bad-url"],
            },
            "arangodb": {"enabled": "yes"},
        }
    )
    assert cfg["stream"]["playlist_stale_seconds"] == 10.0
    assert cfg["stream"]["watchdog_restart_cooldown"] == 5.0
    assert cfg["stream"]["startup_grace_seconds"] == 5.0
    assert cfg["stream"]["encoder"] == "gpu-only"
    assert cfg["stream"]["ffmpeg_log_dir"] == "/tmp/obbystreams-test-logs"
    assert cfg["stream"]["min_assessment_seconds"] == 15.0
    assert cfg["stream"]["health_sample_interval"] == 1.0
    assert cfg["stream"]["confirmed_failure_samples"] == 1
    assert cfg["stream"]["failure_ramp_seconds"] == 15.0
    assert cfg["stream"]["links"] == ["https://ok.example.com/a.m3u8"]
    assert cfg["arangodb"]["enabled"] is True


def test_safe_hls_path_rejects_traversal():
    assert safe_hls_path("ufc.m3u8") == "ufc.m3u8"
    assert safe_hls_path("/ufc001.ts") == "ufc001.ts"
    assert safe_hls_path("../etc/passwd") is None
    assert safe_hls_path("seg/../../x.ts") is None


def test_rewrite_playlist_prefixes_relative_paths():
    text = "#EXTM3U\n#EXTINF:2.0,\nufc01.ts\nhttps://cdn.example.com/ufc02.ts\n"
    rewritten = rewrite_playlist(text)
    assert "/hls/ufc01.ts" in rewritten
    assert "https://cdn.example.com/ufc02.ts" in rewritten


def test_valid_stream_url():
    assert valid_stream_url("https://example.com/live.m3u8")
    assert valid_stream_url("http://example.com/live.m3u8")
    assert not valid_stream_url("ftp://example.com/live.m3u8")
    assert not valid_stream_url("example.com/live.m3u8")


def test_health_scorer_does_not_fail_before_minimum_assessment_window():
    cfg = normalize_config({"stream": {"links": ["https://ok.example.com/a.m3u8"]}})
    scorer = StreamHealthScorer()
    result = scorer.assess(
        cfg,
        {"managed": True, "pid": 123, "started_at": 1, "age": 5.0, "children": [{"pid": 456}]},
        {"playlist_exists": False, "playlist_ready": False, "segments": 0, "bytes": 0, "playlist_age": None},
        force=True,
    )
    assert result["decision"] == "assessing"
    assert result["level"] == "warn"
    assert result["assessment_remaining"] == 10.0


def test_health_scorer_rewards_fresh_hls_output_heavily():
    cfg = normalize_config({"stream": {"links": ["https://ok.example.com/a.m3u8"]}})
    scorer = StreamHealthScorer()
    result = scorer.assess(
        cfg,
        {"managed": True, "pid": 123, "started_at": 1, "age": 20.0, "children": [{"pid": 456}]},
        {
            "playlist_exists": True,
            "playlist_ready": True,
            "playlist_age": 1.0,
            "playlist_modified_at": 1000,
            "segments": 4,
            "bytes": 8_000_000,
            "last_segment_size": 2_000_000,
            "media_sequence": "10",
        },
        force=True,
    )
    assert result["decision"] == "healthy"
    assert result["score"] >= cfg["stream"]["success_score_threshold"]


def test_health_scorer_keeps_fresh_playlist_healthy_between_segment_writes():
    cfg = normalize_config({"stream": {"links": ["https://ok.example.com/a.m3u8"], "playlist_stale_seconds": 25}})
    scorer = StreamHealthScorer()
    proc = {"managed": True, "pid": 123, "started_at": 1, "age": 40.0, "children": [{"pid": 456}]}
    hls = {
        "playlist_exists": True,
        "playlist_ready": True,
        "playlist_age": 2.0,
        "playlist_modified_at": 1000,
        "segments": 10,
        "bytes": 20_000_000,
        "last_segment_size": 2_000_000,
        "media_sequence": "20",
    }
    scorer.assess(cfg, proc, hls, force=True)
    result = scorer.assess(cfg, proc, hls, force=True)
    assert result["decision"] == "healthy"
    assert "no HLS progress since previous sample" not in result["evidence"]["reasons"]


def test_health_scorer_confirms_failure_only_after_repeated_bad_samples():
    cfg = normalize_config({"stream": {"links": ["https://ok.example.com/a.m3u8"], "confirmed_failure_samples": 2}})
    scorer = StreamHealthScorer()
    proc = {"managed": True, "pid": 123, "started_at": 1, "age": 70.0, "children": [{"pid": 456}]}
    hls = {"playlist_exists": False, "playlist_ready": False, "segments": 0, "bytes": 0, "playlist_age": None}
    first = scorer.assess(cfg, proc, hls, force=True)
    second = scorer.assess(cfg, proc, hls, force=True)
    assert first["decision"] != "failed"
    assert second["decision"] == "failed"


def test_build_command_passes_scoring_flags_to_transcoder():
    cfg = normalize_config({"stream": {"links": ["https://ok.example.com/a.m3u8"], "command": "/usr/bin/obbystreams"}})
    cmd = build_command(cfg)
    assert "--ffmpeg-log-dir" in cmd
    assert "--min-assessment-seconds" in cmd
    assert "--success-score-threshold" in cmd
    assert "--failure-score-threshold" in cmd
    assert "--confirmed-failure-samples" in cmd
    assert "--failure-ramp-seconds" in cmd


def test_build_command_passes_strict_gpu_mode_to_transcoder():
    cfg = normalize_config({"stream": {"links": ["https://ok.example.com/a.m3u8"], "encoder": "gpu-only"}})
    cmd = build_command(cfg)
    assert cmd[cmd.index("--encoder") + 1] == "gpu-only"


def test_normalize_config_rejects_unknown_encoder_mode():
    cfg = normalize_config({"stream": {"encoder": "quantum"}})
    assert cfg["stream"]["encoder"] == "auto"


def test_status_lines_do_not_replay_old_errors_as_new_errors():
    assert classify_stream_log("obbystreams status: link 1/3, last ffmpeg: Error opening input") == "info"


def test_watchdog_does_not_restart_after_manual_stop():
    cfg = normalize_config({"stream": {"links": ["https://ok.example.com/a.m3u8"], "auto_restart_on_exit": True}})
    assert should_watchdog_restart_exited_process(cfg, "running")
    assert not should_watchdog_restart_exited_process(cfg, "stopped")


def test_nvidia_smi_parsers_extract_gpu_and_process_metrics():
    gpus = parse_nvidia_gpu_csv(
        "0, NVIDIA RTX 4000, GPU-abc, 550.54, P2, 63, 72, 38, 8192, 2048, 6144, 82.5, 120.0, 1500, 5001\n"
    )
    assert gpus[0]["index"] == 0
    assert gpus[0]["memory_used_pct"] == 25.0
    assert gpus[0]["power_draw_w"] == 82.5

    processes = parse_nvidia_process_csv("GPU-abc, 1234, ffmpeg, 512\n")
    assert processes == [{"gpu_uuid": "GPU-abc", "pid": 1234, "process_name": "ffmpeg", "used_memory_mb": 512}]

    pmon = parse_nvidia_pmon(
        "# gpu pid type sm mem enc dec command\n"
        "0 1234 C 14 8 63 0 ffmpeg\n"
    )
    assert pmon[0]["enc_pct"] == 63
    assert pmon[0]["process_name"] == "ffmpeg"
