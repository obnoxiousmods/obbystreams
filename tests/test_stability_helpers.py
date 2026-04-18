import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import (
    analyze_nvidia_smi,
    merge_nvidia_processes,
    normalize_config,
    normalize_links,
    parse_nvidia_gpu_csv,
    parse_nvidia_pmon,
    rewrite_playlist,
    safe_hls_path,
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
                "links": ["https://ok.example.com/a.m3u8", "https://ok.example.com/a.m3u8", "bad-url"],
            },
            "arangodb": {"enabled": "yes"},
        }
    )
    assert cfg["stream"]["playlist_stale_seconds"] == 10.0
    assert cfg["stream"]["watchdog_restart_cooldown"] == 5.0
    assert cfg["stream"]["startup_grace_seconds"] == 5.0
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


def test_nvidia_smi_parsers_and_analysis_detect_ffmpeg_activity():
    gpus = parse_nvidia_gpu_csv(
        "0, RTX 4090, GPU-abc, 550.54, P0, 61, 72, 22, 24564, 8192, 16372, 240.5, 450.0, 2550, 10501\n"
    )
    processes = merge_nvidia_processes([], parse_nvidia_pmon("# gpu pid type sm mem enc dec command\n0 1234 C 42 15 65 0 ffmpeg\n"), gpus)
    analysis = analyze_nvidia_smi(gpus, processes, {"gpus": {"returncode": 0}})

    assert gpus[0]["memory_used_pct"] == 33.3
    assert processes[0]["is_ffmpeg"] is True
    assert analysis["available"] is True
    assert analysis["summary"]["stream_gpu_active"] is True
    assert analysis["summary"]["ffmpeg_process_count"] == 1
