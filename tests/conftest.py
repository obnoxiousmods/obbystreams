"""Shared pytest configuration and fixtures for the obbystreams backend suite.

Two things live here:

1. A tiny async-test shim (``pytest_pyfunc_call``) that runs coroutine tests via
   ``asyncio.run`` on an ``asyncio`` marker, avoiding a hard dependency on the
   pytest-asyncio plugin.
2. A hermetic config sandbox: ``OBBYSTREAMS_CONFIG`` is redirected to a temp file
   BEFORE ``app`` is imported, so no test ever reads or writes the live
   ``/etc/obbystreams/obbystreams.yaml``. The ``client``/``anon_client`` fixtures
   build a Starlette ``TestClient`` against ``app.app`` WITHOUT entering the
   lifespan (so no ffmpeg, scraper, or ArangoDB background tasks start).
"""

import asyncio
import inspect
import os
import pathlib
import sys
import tempfile

import pytest
import yaml

# Ensure the project root (containing app.py) is importable regardless of how
# pytest is launched (`python -m pytest` vs the `uv run pytest` console script).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# --- Hermetic config sandbox (must run before `import app`) ------------------

_TEST_DIR = tempfile.mkdtemp(prefix="obbystreams-tests-")
_TEST_CONFIG_PATH = pathlib.Path(_TEST_DIR) / "obbystreams.yaml"
os.environ["OBBYSTREAMS_CONFIG"] = str(_TEST_CONFIG_PATH)

#: Auth token the ``client`` fixture presents on every request.
TEST_TOKEN = "testtoken"

#: Minimal, network-free base config used to reset the sandbox per test.
BASE_CONFIG = {
    "dashboard": {"password": "testpass", "session_token": TEST_TOKEN},
    "arangodb": {"enabled": False},
    "private_iptv": {"enabled": False},
    "stream": {"links": [], "sources": []},
}


def _write_base_config():
    with open(_TEST_CONFIG_PATH, "w", encoding="utf-8") as handle:
        yaml.safe_dump(BASE_CONFIG, handle, sort_keys=False)


_write_base_config()


def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: run an async test with asyncio.run")


def pytest_pyfunc_call(pyfuncitem):
    if "asyncio" not in pyfuncitem.keywords:
        return None
    test_fn = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test_fn):
        return None
    kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
    asyncio.run(test_fn(**kwargs))
    return True


# --- Fixtures ----------------------------------------------------------------


def _reset_config_cache():
    import app

    app._CONFIG_CACHE = {"config": None, "mtime": 0.0, "at": 0.0}


@pytest.fixture
def config_path():
    """Path to the sandboxed config YAML (reset to BASE_CONFIG before each use)."""
    _write_base_config()
    _reset_config_cache()
    return _TEST_CONFIG_PATH


@pytest.fixture
def write_config():
    """Return a helper that overwrites the sandbox config with a dict and clears
    the load cache so the next ``load_config()`` re-reads it."""

    def _write(config):
        with open(_TEST_CONFIG_PATH, "w", encoding="utf-8") as handle:
            yaml.safe_dump(config, handle, sort_keys=False)
        _reset_config_cache()

    return _write


@pytest.fixture
def client(config_path):
    """Authenticated Starlette TestClient. Does NOT enter the lifespan, so no
    background ffmpeg/scraper/arango tasks start."""
    from starlette.testclient import TestClient

    import app

    # TestClient's base_url is http://testserver; a matching Origin makes
    # trusted_request_origin() pass for endpoints that check it (e.g. login).
    return TestClient(
        app.app,
        headers={"x-obbystreams-token": TEST_TOKEN, "origin": "http://testserver"},
    )


@pytest.fixture
def anon_client(config_path):
    """Unauthenticated TestClient (no token header) for auth-failure assertions."""
    from starlette.testclient import TestClient

    import app

    return TestClient(app.app)
