"""ASGI integration tests for the obbystreams route surface.

Exercises every HTTP endpoint through a Starlette ``TestClient`` (no lifespan, so
no ffmpeg/scraper/arango tasks run). An autouse fixture neutralises every
external side effect — process spawning/killing, upstream fetches, geo lookups,
ArangoDB — so the suite is hermetic and safe to run on the live host.

Focus areas: the auth boundary on guarded routes, and the new persistent Stop +
blacklist behaviour, plus a happy-path smoke over the remaining routes.
"""

import pytest


@pytest.fixture(autouse=True)
def neutralize_externals(monkeypatch):
    import app

    async def _async_noop(*args, **kwargs):
        return False

    async def _fake_restart(reason):
        return True

    async def _fake_scrape_page(url):
        return []

    async def _fake_record_watch(*args, **kwargs):
        return None

    async def _fake_arango_request(method, path, **kwargs):
        return {"server": "arango", "version": "test"}

    def _fake_start(config, links, kill_existing=True):
        return 4321, ["ffmpeg", "-i", "test"]

    monkeypatch.setattr(app, "stop_managed_process", _async_noop)
    monkeypatch.setattr(app, "start_managed_process", _fake_start)
    monkeypatch.setattr(app, "restart_managed_with_config", _fake_restart)
    monkeypatch.setattr(app, "kill_existing_streams", list)
    monkeypatch.setattr(app, "_scrape_page", _fake_scrape_page)
    monkeypatch.setattr(app, "record_watch", _fake_record_watch)
    monkeypatch.setattr(app, "arango_request", _fake_arango_request)
    monkeypatch.setattr(app, "PROCESS", None, raising=False)
    yield


# --- Health / auth boundary --------------------------------------------------


def test_health_is_public(client):
    resp = client.get("/api/health")
    assert resp.status_code in (200, 503)
    assert "ok" in resp.json()


GUARDED_GET = ["/api/status", "/api/sources", "/api/private-iptv", "/api/config", "/api/blacklist", "/api/arango", "/api/nvidia-smi"]
GUARDED_POST = [
    "/api/sources/activate",
    "/api/private-iptv/refresh",
    "/api/links",
    "/api/links/remove",
    "/api/public-streams",
    "/api/public-streams/remove",
    "/api/blacklist",
    "/api/blacklist/remove",
    "/api/stream/start",
    "/api/stream/stop",
    "/api/stream/restart",
]


@pytest.mark.parametrize("path", GUARDED_GET)
def test_guarded_get_requires_auth(anon_client, path):
    assert anon_client.get(path).status_code == 401


@pytest.mark.parametrize("path", GUARDED_POST)
def test_guarded_post_requires_auth(anon_client, path):
    # No token header and no trusted origin -> 403 (origin) or 401 (auth).
    assert anon_client.post(path, json={}).status_code in (401, 403)


def test_login_accepts_correct_password(client):
    resp = client.post("/api/auth/login", json={"password": "testpass"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_login_rejects_wrong_password(client):
    resp = client.post("/api/auth/login", json={"password": "nope"})
    assert resp.status_code in (401, 403)


# --- Read endpoints ----------------------------------------------------------


def test_status_payload_shape(client):
    data = client.get("/api/status").json()
    assert data["ok"] is True
    assert "runtime" in data
    assert data["runtime"]["operator_stopped"] is False
    assert "source_blacklist" in data["config"]


def test_public_read_endpoints(client):
    for path in ["/api/public-streams", "/api/public-configured-sources", "/api/news", "/api/highscores",
                 "/api/viewers", "/api/ufc-schedule"]:
        assert client.get(path).status_code == 200


def test_sources_and_private_iptv_and_config(client):
    assert client.get("/api/sources").json()["ok"] is True
    assert client.get("/api/private-iptv").status_code == 200
    assert client.get("/api/config").json()["ok"] is True
    assert client.get("/api/arango").json()["ok"] is True
    assert client.get("/api/nvidia-smi").status_code == 200


def test_proxy_hls_rejects_bad_url(client):
    assert client.get("/api/proxy-hls").status_code == 400
    assert client.get("/api/proxy-hls?url=not-a-url").status_code == 400


def test_source_hls_unknown_id(client):
    assert client.get("/api/source-hls/does-not-exist").status_code in (400, 404)


# --- Persistent Stop / Start via the API -------------------------------------


def test_stop_start_persist_operator_flag(client):
    import app

    stop = client.post("/api/stream/stop", json={})
    assert stop.status_code == 200
    assert stop.json()["operator_stopped"] is True
    assert app.load_config(fresh=True)["stream"]["operator_stopped"] is True

    # A fresh status read reflects it.
    assert client.get("/api/status").json()["runtime"]["operator_stopped"] is True

    # Start clears it (needs a link so start_managed_process is reached).
    client.put("/api/config", json={"sources": ["https://a.example.com/live.m3u8"]})
    start = client.post("/api/stream/start", json={})
    assert start.status_code == 200
    assert app.load_config(fresh=True)["stream"]["operator_stopped"] is False


def test_restart_clears_operator_flag(client):
    import app

    client.post("/api/stream/stop", json={})
    assert app.load_config(fresh=True)["stream"]["operator_stopped"] is True
    client.put("/api/config", json={"sources": ["https://a.example.com/live.m3u8"]})
    resp = client.post("/api/stream/restart", json={})
    assert resp.status_code == 200
    assert app.load_config(fresh=True)["stream"]["operator_stopped"] is False


def test_stop_records_a_manual_reason_and_start_clears_it(client):
    """The banner distinguishes a deliberate Stop from a scheduled standby."""
    import app

    stop = client.post("/api/stream/stop", json={})
    assert stop.json()["stop_reason"] == "manual"
    assert app.load_config(fresh=True)["stream"]["stop_reason"] == "manual"
    assert client.get("/api/status").json()["runtime"]["stop_reason"] == "manual"

    client.put("/api/config", json={"sources": ["https://a.example.com/live.m3u8"]})
    client.post("/api/stream/start", json={})
    assert app.load_config(fresh=True)["stream"]["stop_reason"] == ""


def test_stop_reason_is_empty_while_running(client):
    import app

    config = app.load_config(fresh=True)
    config["stream"]["operator_stopped"] = False
    config["stream"]["stop_reason"] = "schedule"
    assert app.stop_reason(config) == ""


# --- Auto-schedule endpoints -------------------------------------------------


def test_schedule_requires_auth(anon_client):
    assert anon_client.get("/api/schedule").status_code == 401
    # POSTs also fail the origin check, so an unauthenticated write is 401 or 403.
    assert anon_client.post("/api/schedule", json={"enabled": False}).status_code in (401, 403)


def test_schedule_get_returns_a_snapshot(client):
    body = client.get("/api/schedule").json()
    assert body["ok"] is True
    assert "enabled" in body["schedule"]
    assert "phase" in body["schedule"]


def test_schedule_toggle_persists(client):
    import app

    off = client.post("/api/schedule", json={"enabled": False})
    assert off.status_code == 200
    assert app.load_config(fresh=True)["schedule"]["enabled"] is False

    client.post("/api/schedule", json={"enabled": True})
    assert app.load_config(fresh=True)["schedule"]["enabled"] is True


def test_schedule_section_survives_an_unrelated_save(client):
    """normalize_config rebuilds from DEFAULT_CONFIG; schedule must not be dropped."""
    import app

    client.post("/api/schedule", json={"enabled": False})
    client.put("/api/config", json={"sources": ["https://a.example.com/live.m3u8"]})

    assert app.load_config(fresh=True)["schedule"]["enabled"] is False


def test_schedule_appears_in_the_status_payload(client):
    assert "schedule" in client.get("/api/status").json()


def test_test_notification_without_a_webhook_is_rejected(client):
    resp = client.post("/api/schedule", json={"test_notification": True})
    # No scheduler in the test harness (lifespan is never entered), so this is a
    # 503; with a scheduler but no webhook it would be a 400. Either way: refused.
    assert resp.status_code in (400, 503)


def test_webhook_url_is_redacted_from_the_config_api(client):
    import app

    config = app.load_config(fresh=True)
    config["schedule"]["notify"]["discord_webhook_url"] = "https://discord.com/api/webhooks/123/supersecret"
    app.save_config(config)

    exposed = client.get("/api/config").json()["config"]["schedule"]["notify"]["discord_webhook_url"]
    assert exposed == "***"
    assert "supersecret" not in client.get("/api/status").text


# --- Blacklist endpoints -----------------------------------------------------


def test_blacklist_add_list_remove_roundtrip(client):
    add = client.post("/api/blacklist", json={"url": "https://blocked.example/live.m3u8", "reason": "slate"})
    assert add.status_code == 200
    listed = client.get("/api/blacklist").json()["blacklist"]
    assert any(e["url"] == "https://blocked.example/live.m3u8" for e in listed)

    remove = client.post("/api/blacklist/remove", json={"url": "https://blocked.example/live.m3u8"})
    assert remove.status_code == 200
    assert client.get("/api/blacklist").json()["blacklist"] == []


def test_blacklist_add_requires_a_selector(client):
    assert client.post("/api/blacklist", json={}).status_code == 400


def test_blacklist_add_strips_matching_public_source(client):
    client.post("/api/public-streams", json={"url": "https://blocked.example/live.m3u8", "label": "Bad"})
    assert any(s["url"] == "https://blocked.example/live.m3u8" for s in client.get("/api/public-streams").json().get("sources", []))
    client.post("/api/blacklist", json={"url": "https://blocked.example/live.m3u8"})
    urls = [s["url"] for s in client.get("/api/public-streams").json().get("sources", [])]
    assert "https://blocked.example/live.m3u8" not in urls


def test_add_public_stream_rejects_blacklisted(client):
    client.post("/api/blacklist", json={"url": "https://blocked.example/live.m3u8"})
    resp = client.post("/api/public-streams", json={"url": "https://blocked.example/live.m3u8"})
    assert resp.status_code == 409


# --- Config / links / news / viewers write paths -----------------------------


def test_put_config_updates_public_sources(client):
    resp = client.put("/api/config", json={"public_sources": [{"id": "x", "url": "https://a.example.com/live.m3u8"}]})
    assert resp.status_code == 200
    cfg = client.get("/api/config").json()["config"]
    assert any(s["url"] == "https://a.example.com/live.m3u8" for s in cfg["public_sources"])


def test_links_add_and_remove(client):
    client.post("/api/links", json={"url": "https://a.example.com/live.m3u8"})
    cfg = client.get("/api/config").json()["config"]
    assert "https://a.example.com/live.m3u8" in (cfg["stream"].get("links") or [])
    client.post("/api/links/remove", json={"url": "https://a.example.com/live.m3u8"})
    cfg = client.get("/api/config").json()["config"]
    assert "https://a.example.com/live.m3u8" not in (cfg["stream"].get("links") or [])


def test_news_add_and_remove(client):
    add = client.post("/api/news", json={"title": "Test", "body": "Hello"})
    assert add.status_code == 200
    entry_id = add.json()["entry"]["id"]
    remove = client.post("/api/news/remove", json={"id": entry_id})
    assert remove.status_code == 200


def test_viewers_post_records_session(client):
    resp = client.post("/api/viewers", json={"session_id": "abc", "source_id": "server-1"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_coming_up_without_a_scheduler_is_refused(client):
    resp = client.post("/api/schedule", json={"coming_up": True})
    assert resp.status_code in (400, 404, 503)


def test_ufc_schedule_is_public_and_needs_no_token(anon_client):
    # The watcher is a static site on a different origin with no credentials, so
    # this has to work unauthenticated or the schedule silently never loads.
    response = anon_client.get("/api/ufc-schedule")
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"


def test_ufc_schedule_degrades_instead_of_failing_without_a_scheduler(anon_client):
    # Tests run with no scheduler; so does a cold boot. The site must render
    # something rather than error.
    body = anon_client.get("/api/ufc-schedule").json()
    assert body["ok"] is False
    assert body["event"] is None
    assert body["upcoming"] == []


def test_ufc_schedule_never_leaks_scheduler_internals(anon_client):
    # Veto state, source matching and the notification ledger say nothing to a
    # viewer and should not be on a public endpoint.
    body = anon_client.get("/api/ufc-schedule").json()
    for leaked in ("source_state", "suppressed_event_id", "notifications_sent", "context", "lifecycle"):
        assert leaked not in body
