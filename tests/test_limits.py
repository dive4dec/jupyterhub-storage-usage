"""Tests for per-user/group limit overrides."""

import json
import os
import tempfile
import pytest
from jupyterhub_storage_usage.limits import resolve_limits


@pytest.fixture
def overrides_file(tmp_path):
    """Create a temporary overrides JSON file."""
    overrides = {
        "users": {
            "alice": {"warn": "30Gi", "hard": "60Gi"},
            "bob": {"warn": "5Gi", "hard": "10Gi"},
        },
        "groups": {
            "project": {"warn": "20Gi", "hard": "50Gi"},
            "grader": {"warn": "50Gi", "hard": "100Gi"},
        },
    }
    f = tmp_path / "overrides.json"
    f.write_text(json.dumps(overrides))
    old = os.environ.get("JSU_LIMIT_OVERRIDES_PATH", "")
    os.environ["JSU_LIMIT_OVERRIDES_PATH"] = str(f)
    yield f
    os.environ["JSU_LIMIT_OVERRIDES_PATH"] = old


def test_user_override(overrides_file):
    """User-specific override takes priority."""
    warn, hard = resolve_limits("alice", [], 10 * 1024**3, 20 * 1024**3)
    assert warn == 30 * 1024**3
    assert hard == 60 * 1024**3


def test_group_override(overrides_file):
    """Group override applies when no user override."""
    warn, hard = resolve_limits("charlie", ["project"], 10 * 1024**3, 20 * 1024**3)
    assert warn == 20 * 1024**3
    assert hard == 50 * 1024**3


def test_default_when_no_override(overrides_file):
    """Global default when no user or group override."""
    warn, hard = resolve_limits("nobody", [], 10 * 1024**3, 20 * 1024**3)
    assert warn == 10 * 1024**3
    assert hard == 20 * 1024**3


def test_user_overrides_group(overrides_file):
    """User override takes priority over group override."""
    warn, hard = resolve_limits("alice", ["project"], 10 * 1024**3, 20 * 1024**3)
    assert warn == 30 * 1024**3  # alice's user override, not project's
    assert hard == 60 * 1024**3


def test_quantity_strings(overrides_file):
    """Overrides accept K8s quantity strings."""
    warn, hard = resolve_limits("bob", [], 10 * 1024**3, 20 * 1024**3)
    assert warn == 5 * 1024**3  # 5Gi
    assert hard == 10 * 1024**3  # 10Gi


def test_no_overrides_file():
    """No overrides file → global default."""
    old = os.environ.pop("JSU_LIMIT_OVERRIDES_PATH", "")
    try:
        warn, hard = resolve_limits("anyone", ["anygroup"], 10, 20)
        assert warn == 10
        assert hard == 20
    finally:
        if old:
            os.environ["JSU_LIMIT_OVERRIDES_PATH"] = old
