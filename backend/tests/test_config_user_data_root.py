"""Regression tests for Bug #3: dev mode must honor LN_TRANSLATOR_DATA.

backend.config.USER_DATA_ROOT is computed at import time, so testing the
env-var override requires a fresh Python process — conftest's pre-import
override of DB_PATH would otherwise mask anything we set here.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _resolve_user_data_root(env: dict) -> str:
    """Spawn a fresh interpreter with `env`, import backend.config, print
    USER_DATA_ROOT. Returns the printed path, stripped."""
    # Clear DB_PATH so the override-check inside config.py uses the
    # USER_DATA_ROOT we're trying to test rather than a separate DB_PATH env.
    env = dict(env)
    env.pop("DB_PATH", None)
    # Run from the repo root so backend.* imports resolve without PYTHONPATH.
    repo_root = Path(__file__).resolve().parent.parent.parent
    result = subprocess.run(
        [sys.executable, "-c", "from backend.config import USER_DATA_ROOT; print(USER_DATA_ROOT)"],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"subprocess exited {result.returncode}\nstdout:{result.stdout}\nstderr:{result.stderr}"
    )
    return result.stdout.strip().splitlines()[-1]


def test_dev_mode_honors_LN_TRANSLATOR_DATA():
    """The headline fix. Without LN_TRANSLATOR_DATA set, dev mode points at
    repo/data; with it set, the override wins."""
    override = Path(tempfile.mkdtemp(prefix="config-override-"))
    env = os.environ.copy()
    env["LN_TRANSLATOR_DATA"] = str(override)
    resolved = _resolve_user_data_root(env)
    # Normalise path separators for cross-platform comparison.
    assert Path(resolved) == override, (
        f"USER_DATA_ROOT expected {override}, got {resolved}"
    )


def test_dev_mode_default_is_repo_data():
    """Sanity check: without the override, dev mode still points at repo/data."""
    env = os.environ.copy()
    env.pop("LN_TRANSLATOR_DATA", None)
    resolved = _resolve_user_data_root(env)
    repo_root = Path(__file__).resolve().parent.parent.parent
    assert Path(resolved) == repo_root / "data", (
        f"expected {repo_root / 'data'}, got {resolved}"
    )


def test_LN_TRANSLATOR_DATA_expands_user():
    """`~` in the override path is expanded so users can pass paths like
    `~/scratch` directly."""
    env = os.environ.copy()
    env["LN_TRANSLATOR_DATA"] = "~/ln-translator-test-expand"
    resolved = _resolve_user_data_root(env)
    expected = Path("~/ln-translator-test-expand").expanduser()
    assert Path(resolved) == expected
