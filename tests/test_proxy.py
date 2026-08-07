import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app import _proxy_url, _ProxyCache, _rewrite_m3u8, _split_curl_headers_body, assess_playback_candidate, normalize_config


@pytest.mark.asyncio
async def test_proxy_cache_basic_set_get():
    cache = _ProxyCache(max_size=10, playlist_ttl=1.0, segment_ttl=2.0)
    await cache.set("url1", b"body", "video/mp2t", 1.5, 0.0)
    assert await cache.get("url1", 0.5) == (b"body", "video/mp2t")
    assert await cache.get("url1", 2.0) is None


@pytest.mark.asyncio
async def test_proxy_cache_respects_supplied_ttl():
    cache = _ProxyCache(max_size=10, playlist_ttl=1.0, segment_ttl=2.0)
    await cache.set("url1", b"body", "video/mp2t", 5.0, 0.0)
    assert await cache.get("url1", 4.0) == (b"body", "video/mp2t")
    assert await cache.get("url1", 6.0) is None


@pytest.mark.asyncio
async def test_proxy_cache_size_bound_evicts_oldest_half():
    cache = _ProxyCache(max_size=4, playlist_ttl=10.0, segment_ttl=10.0)
    for i in range(4):
        await cache.set(f"url{i}", f"body{i}".encode(), "video/mp2t", 10.0, float(i))
    # Adding a 5th entry triggers eviction of the oldest half.
    await cache.set("url4", b"body4", "video/mp2t", 10.0, 4.0)
    assert await cache.get("url0", 5.0) is None
    assert await cache.get("url1", 5.0) is None
    assert await cache.get("url2", 5.0) == (b"body2", "video/mp2t")
    assert await cache.get("url4", 5.0) == (b"body4", "video/mp2t")


@pytest.mark.asyncio
async def test_proxy_cache_cleanup_removes_expired():
    import time

    cache = _ProxyCache(max_size=10, playlist_ttl=1.0, segment_ttl=2.0)
    now = time.monotonic()
    await cache.set("old", b"old", "video/mp2t", 0.001, now)
    await cache.set("new", b"new", "video/mp2t", 3600.0, now)
    # Ensure 'old' has expired in real time before cleanup runs.
    await asyncio.sleep(0.01)
    await cache.cleanup()
    assert await cache.get("old", time.monotonic()) is None
    assert await cache.get("new", time.monotonic()) == (b"new", "video/mp2t")


@pytest.mark.asyncio
async def test_proxy_cache_inflight_lock_released():
    cache = _ProxyCache(max_size=10, playlist_ttl=1.0, segment_ttl=2.0)
    lock = cache.lock("url1")
    async with lock:
        pass
    await cache.release_lock("url1", lock)
    await cache.cleanup()
    # After release and cleanup the lock should be gone.
    assert "url1" not in cache._inflight


@pytest.mark.asyncio
async def test_proxy_cache_keeps_stale_entry_after_fresh_ttl():
    cache = _ProxyCache(max_size=10, playlist_ttl=1.0, segment_ttl=2.0, stale_ttl=10.0)
    await cache.set("url1", b"body", "video/mp2t", 1.0, 0.0)
    assert await cache.get("url1", 2.0) is None
    assert await cache.get_stale("url1", 2.0) == (b"body", "video/mp2t")
    assert await cache.get_stale("url1", 20.0) is None


@pytest.mark.asyncio
async def test_proxy_cache_does_not_release_lock_acquired_by_waiter():
    cache = _ProxyCache(max_size=10, playlist_ttl=1.0, segment_ttl=2.0)
    lock = cache.lock("url1")
    await lock.acquire()

    waiter_acquired = asyncio.Event()

    async def waiter():
        async with lock:
            waiter_acquired.set()
            await asyncio.sleep(0.02)

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    lock.release()
    await waiter_acquired.wait()
    await cache.release_lock("url1", lock)
    assert cache.lock("url1") is lock
    await task
    await cache.release_lock("url1", lock)
    assert "url1" not in cache._inflight


def test_proxy_url_encodes_url():
    built = _proxy_url("https://example.com/stream.m3u8?token=a/b")

    assert built.startswith(
        "/api/proxy-hls?url=https%3A%2F%2Fexample.com%2Fstream.m3u8%3Ftoken%3Da%2Fb"
    )


def test_proxy_url_is_signed_and_verifies_only_for_that_exact_url():
    """The signature is the only thing standing between this endpoint and being
    an open web proxy, so it has to be bound to the URL, not just present."""
    from urllib.parse import parse_qs, urlparse

    from app import _proxy_signature_valid

    target = "https://example.com/stream.m3u8"
    query = parse_qs(urlparse(_proxy_url(target)).query)
    exp, sig = query["exp"][0], query["sig"][0]

    assert _proxy_signature_valid(target, exp, sig)
    assert not _proxy_signature_valid("https://evil.example/x.m3u8", exp, sig)
    assert not _proxy_signature_valid(target, exp, "0" * 32)
    assert not _proxy_signature_valid(target, "0", sig), "an expired link still verified"


def test_rewrite_m3u8_rewrites_relative_segments():
    playlist = (
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n"
        "#EXTINF:4.0,\nsegment0.ts\n#EXTINF:4.0,\nsegment1.ts\n#EXT-X-ENDLIST\n"
    )
    raw_url = "https://example.com/live/playlist.m3u8?key=abc"
    rewritten = _rewrite_m3u8(playlist, raw_url)
    assert "/api/proxy-hls?url=https%3A%2F%2Fexample.com%2Flive%2Fsegment0.ts" in rewritten
    assert "/api/proxy-hls?url=https%3A%2F%2Fexample.com%2Flive%2Fsegment1.ts" in rewritten
    assert "#EXTM3U" in rewritten


def test_rewrite_m3u8_rewrites_key_uri():
    playlist = (
        '#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
        "#EXTINF:4.0,\nsegment.ts\n"
    )
    raw_url = "https://example.com/live/playlist.m3u8"
    rewritten = _rewrite_m3u8(playlist, raw_url)
    assert "/api/proxy-hls?url=https%3A%2F%2Fexample.com%2Flive%2Fkey.bin" in rewritten


def test_rewrite_m3u8_preserves_existing_proxy_urls():
    playlist = "#EXTM3U\n#EXTINF:4.0,\n/api/proxy-hls?url=https%3A%2F%2Fexample.com%2Fseg.ts\n"
    raw_url = "https://example.com/live/playlist.m3u8"
    rewritten = _rewrite_m3u8(playlist, raw_url)
    assert rewritten.count("/api/proxy-hls") == 1


def test_split_curl_headers_body_uses_final_header_block_after_redirects():
    raw = (
        b"HTTP/1.1 302 Found\r\nlocation: https://cdn.example/live.m3u8\r\n\r\n"
        b"HTTP/2 200\r\ncontent-type: application/vnd.apple.mpegurl\r\n\r\n"
        b"#EXTM3U\n#EXTINF:4,\nseg.ts\n"
    )
    body, content_type = _split_curl_headers_body(raw)
    assert body.startswith(b"#EXTM3U")
    assert content_type == "application/vnd.apple.mpegurl"


@pytest.mark.asyncio
async def test_assess_playback_candidate_rejects_html_playlist_response(monkeypatch):
    async def fake_fetch(url, headers, timeout=10.0):
        return 200, "application/vnd.apple.mpegurl", b"<html><body>blocked</body></html>"

    import app as obbystreams_app

    monkeypatch.setattr(obbystreams_app, "fetch_small_head", fake_fetch)
    cfg = normalize_config({})["private_iptv"]
    result = await assess_playback_candidate("https://example.com/live.m3u8", cfg)
    assert result["ok"] is False
    assert "html response" in result["reasons"]


@pytest.mark.asyncio
async def test_assess_playback_candidate_accepts_media_playlist(monkeypatch):
    async def fake_fetch(url, headers, timeout=10.0):
        if url.endswith(".ts"):
            return 200, "video/mp2t", b"\x47" * 188
        return (
            200,
            "application/vnd.apple.mpegurl",
            b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\nseg0.ts\n#EXTINF:4,\nseg1.ts\n",
        )

    import app as obbystreams_app

    monkeypatch.setattr(obbystreams_app, "fetch_small_head", fake_fetch)
    cfg = normalize_config({})["private_iptv"]
    result = await assess_playback_candidate("https://example.com/live.m3u8", cfg)
    assert result["ok"] is True
    assert "media segments" in result["reasons"]
    assert "segment readable" in result["reasons"]


def test_access_log_redacts_provider_tokens():
    """Proxy URLs are signed provider links - bearer credentials with hours of
    life - and uvicorn's access logger writes the whole request line. The journal
    held hundreds of them in cleartext."""
    import logging

    from app import _RedactProxyTargets

    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/1.1" %d',
        ("1.2.3.4:0", "GET",
         "/api/proxy-hls?url=https%3A%2F%2Fcdn.example%2Fsecure%2FSECRETTOKEN%2Fx.m3u8", 200),
        None,
    )

    assert _RedactProxyTargets().filter(record)
    rendered = record.getMessage()

    assert "SECRETTOKEN" not in rendered, f"token survived redaction: {rendered}"
    assert "url=<redacted>" in rendered
    assert "/api/proxy-hls" in rendered, "redaction destroyed the useful part of the line"
    assert "200" in rendered


def test_split_curl_headers_body_does_not_split_on_binary_payload():
    """An MPEG-TS segment can contain b"\\r\\n\\r\\n" by coincidence. Scanning
    backwards for the last one truncated the segment, and the truncated bytes
    were then cached and served to every viewer for 120 seconds."""
    from app import _split_curl_headers_body

    payload = b"\x47\x40\x11\x10" + b"\r\n\r\n" + b"\xff" * 64
    raw = b"HTTP/2 200\r\ncontent-type: video/mp2t\r\n\r\n" + payload

    body, content_type = _split_curl_headers_body(raw)

    assert body == payload, "binary payload was truncated at an embedded separator"
    assert content_type == "video/mp2t"


def test_split_curl_headers_body_skips_a_100_continue_preamble():
    from app import _split_curl_headers_body

    raw = b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\ncontent-type: text/plain\r\n\r\nBODY"

    body, content_type = _split_curl_headers_body(raw)

    assert body == b"BODY"
    assert content_type == "text/plain"
