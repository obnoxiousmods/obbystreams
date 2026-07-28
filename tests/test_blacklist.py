"""Tests for the persistent source blacklist.

Covers normalization, the ``is_blacklisted`` matcher (URL / query-rotation / id /
channel / label), and each scraper funnel filter (candidate selection, the
merge write-barrier, and the viewer-facing public inventory).
"""

from app import (
    auto_public_sources,
    blacklist_index,
    is_blacklisted,
    merge_private_iptv_sources,
    normalize_blacklist,
    normalize_config,
    parse_m3u_entries,
    public_stream_inventory,
    select_private_iptv_candidates,
)


def test_normalize_blacklist_dedupes_and_shapes_entries():
    entries = normalize_blacklist(
        [
            "https://blocked.example/live.m3u8",
            {"url": "https://blocked.example/live.m3u8"},  # duplicate
            {"id": "private-iptv-ufc", "label": "UFC PPV", "channel": "UFC 24/7", "reason": "slate"},
            {},  # empty -> dropped
        ]
    )
    assert len(entries) == 2
    assert entries[0]["url"] == "https://blocked.example/live.m3u8"
    assert entries[1]["id"] == "private-iptv-ufc"
    assert entries[1]["reason"] == "slate"


def test_is_blacklisted_matches_by_url_id_channel_label():
    bl = [
        {"url": "https://blocked.example/live.m3u8"},
        {"id": "private-iptv-bad"},
        {"channel": "UFC 24/7"},
        {"label": "Fake PPV"},
    ]
    index = blacklist_index(bl)
    assert is_blacklisted("https://blocked.example/live.m3u8", index)
    assert is_blacklisted({"id": "private-iptv-bad", "url": "https://other/x.m3u8"}, index)
    assert is_blacklisted({"attrs": {"tvg-name": "ufc 24/7"}, "url": "https://x/y.m3u8"}, index)
    assert is_blacklisted({"title": "fake ppv", "url": "https://x/z.m3u8"}, index)
    assert not is_blacklisted("https://allowed.example/live.m3u8", index)


def test_is_blacklisted_survives_query_token_rotation():
    # Same path, different CDN token in the query string still matches.
    bl = [{"url": "https://cdn.example/stream/index.m3u8?token=OLD"}]
    index = blacklist_index(bl)
    assert is_blacklisted("https://cdn.example/stream/index.m3u8?token=NEW", index)


def test_is_blacklisted_empty_blacklist_is_false():
    assert is_blacklisted("https://x/y.m3u8", []) is False
    assert is_blacklisted("https://x/y.m3u8", set()) is False


def test_query_keyed_host_with_empty_path_does_not_over_match():
    # Blocking one query-keyed stream on a host must NOT block a different query
    # on the same host when the path is empty (the query is the only identity).
    index = blacklist_index([{"url": "https://cdn.example/?stream=abc"}])
    assert is_blacklisted("https://cdn.example/?stream=abc", index)
    assert not is_blacklisted("https://cdn.example/?stream=xyz", index)
    assert not is_blacklisted("https://cdn.example/other.m3u8", index)


def test_id_block_does_not_match_unrelated_label():
    # An id-block (e.g. a positional auto id) must not match a different source
    # whose label happens to equal that id string.
    index = blacklist_index([{"id": "auto-public-3"}])
    assert is_blacklisted({"id": "auto-public-3", "url": "https://x/y.m3u8"}, index)
    assert not is_blacklisted({"label": "auto-public-3", "url": "https://other/z.m3u8"}, index)


def test_channel_and_label_are_interchangeable_names():
    # A human name blocks whether it appears as a source's channel (tvg-name) or
    # its label/title.
    index = blacklist_index([{"channel": "UFC 24/7"}])
    assert is_blacklisted({"attrs": {"tvg-name": "ufc 24/7"}, "url": "https://a/x.m3u8"}, index)
    assert is_blacklisted({"title": "UFC 24/7", "url": "https://b/y.m3u8"}, index)


def test_select_candidates_drops_blacklisted():
    playlist = """#EXTM3U
#EXTINF:-1 tvg-name="UFC PPV Main Card" group-title="PPV Live Events",UFC PPV Main Card
https://soursignal.com/private/keep
#EXTINF:-1 tvg-name="UFC Fight Night" group-title="PPV Live Events",UFC Fight Night
https://soursignal.com/private/blocked
"""
    cfg = normalize_config({"private_iptv": {"min_score": 70}})["private_iptv"]
    entries = parse_m3u_entries(playlist)
    selected = select_private_iptv_candidates(entries, cfg, blacklist=[{"url": "https://soursignal.com/private/blocked"}])
    urls = [item["entry"]["url"] for item in selected]
    assert "https://soursignal.com/private/blocked" not in urls
    assert "https://soursignal.com/private/keep" in urls


def test_merge_write_barrier_rejects_blacklisted_accepted_entry():
    cfg = normalize_config(
        {
            "source_blacklist": [{"url": "https://soursignal.com/private/blocked"}],
            "private_iptv": {"auto_source_prefix": "private-iptv"},
        }
    )
    accepted = [
        {"entry": {"title": "UFC Keep", "attrs": {}, "url": "https://soursignal.com/private/keep"}, "score": 95, "reasons": []},
        {"entry": {"title": "UFC Blocked", "attrs": {}, "url": "https://soursignal.com/private/blocked"}, "score": 95, "reasons": []},
    ]
    merge_private_iptv_sources(cfg, accepted)
    urls = [s["url"] for s in cfg["stream"]["sources"]]
    assert "https://soursignal.com/private/keep" in urls
    assert "https://soursignal.com/private/blocked" not in urls


def test_public_inventory_hides_blacklisted(monkeypatch):
    import app

    monkeypatch.setattr(app, "_AUTO_SOURCES", ["https://auto.example/a.m3u8", "https://auto.example/blocked.m3u8"])
    cfg = normalize_config(
        {
            "source_blacklist": [{"url": "https://auto.example/blocked.m3u8"}, {"url": "https://manual.example/blocked.m3u8"}],
            "public_sources": [
                {"id": "keep", "url": "https://manual.example/keep.m3u8"},
                {"id": "blocked", "url": "https://manual.example/blocked.m3u8"},
            ],
        }
    )
    urls = {s["url"] for s in public_stream_inventory(cfg)}
    assert "https://manual.example/keep.m3u8" in urls
    assert "https://auto.example/a.m3u8" in urls
    assert "https://manual.example/blocked.m3u8" not in urls
    assert "https://auto.example/blocked.m3u8" not in urls
    # auto_public_sources itself is unfiltered (raw); the inventory is the gate.
    assert any(s["url"] == "https://auto.example/blocked.m3u8" for s in auto_public_sources())
