import runpy
from pathlib import Path


RUNNER = runpy.run_path(str(Path(__file__).resolve().parents[1] / "bin" / "obbystreams"))


def test_runner_detects_private_capacity_errors():
    is_capacity_limited_error = RUNNER["is_capacity_limited_error"]

    assert is_capacity_limited_error("HTTP 429 too many requests")
    assert is_capacity_limited_error("provider says too many streams active")
    assert is_capacity_limited_error("concurrent connection limit exceeded")
    assert not is_capacity_limited_error("404 not found")
