"""Tests for jupyterhub-storage-usage cache and spawn hook."""

import os
import tempfile
import time

import pytest

os.environ["JSU_CACHE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["JSU_HARD_LIMIT_BYTES"] = "200"
os.environ["JSU_WARN_LIMIT_BYTES"] = "100"
os.environ["JSU_CLEANUP_IMAGE"] = "registry.test/cleanup:0.1"

from jupyterhub_storage_usage.cache import UsageCache
from jupyterhub_storage_usage.spawn import make_pre_spawn_hook, CLEANUP_IMAGE


# ── Cache tests (unchanged) ─────────────────────────────────────────────

def test_cache_update_and_get():
    cache = UsageCache(os.environ["JSU_CACHE_DB_PATH"])
    cache.update("alice", 50, "ok")
    data = cache.get("alice")
    assert data is not None
    assert data["username"] == "alice"
    assert data["used_bytes"] == 50


def test_cache_get_all_sorted():
    cache = UsageCache(os.environ["JSU_CACHE_DB_PATH"] + "_2")
    cache.update("alice", 50, "ok")
    cache.update("bob", 150, "warning")
    cache.update("carol", 250, "over_limit")
    all_data = cache.get_all()
    assert len(all_data) == 3
    assert all_data[0]["used_bytes"] == 250


def test_cache_get_over_limit():
    cache = UsageCache(os.environ["JSU_CACHE_DB_PATH"] + "_3")
    cache.update("alice", 50, "ok")
    cache.update("bob", 250, "over_limit")
    over = cache.get_over_limit()
    assert len(over) == 1
    assert over[0]["username"] == "bob"


def test_cache_first_over_limit_tracking():
    cache = UsageCache(os.environ["JSU_CACHE_DB_PATH"] + "_4")
    cache.update("alice", 50, "ok")
    assert cache.get("alice")["first_over_limit"] == 0.0

    cache.update("alice", 250, "over_limit")
    first = cache.get("alice")["first_over_limit"]
    assert first > 0

    time.sleep(0.1)
    cache.update("alice", 260, "over_limit")
    assert cache.get("alice")["first_over_limit"] == first

    cache.update("alice", 50, "ok")
    assert cache.get("alice")["first_over_limit"] == 0.0


def test_cache_remove():
    cache = UsageCache(os.environ["JSU_CACHE_DB_PATH"] + "_5")
    cache.update("alice", 50, "ok")
    assert cache.get("alice") is not None
    cache.remove("alice")
    assert cache.get("alice") is None


def test_cache_cleanup_stale():
    cache = UsageCache(os.environ["JSU_CACHE_DB_PATH"] + "_6")
    cache.update("alice", 50, "ok")
    import sqlite3
    with sqlite3.connect(os.environ["JSU_CACHE_DB_PATH"] + "_6") as conn:
        conn.execute("UPDATE usage SET timestamp=? WHERE username=?", (time.time() - 100000, "alice"))
        conn.commit()
    cache.cleanup_stale(max_age_sec=86400)
    assert cache.get("alice") is None


# ── Pre-spawn hook tests (new: swaps image instead of blocking) ─────────

class FakeUser:
    def __init__(self, name, admin=False, groups=None):
        self.name = name
        self.admin = admin
        self.groups = groups or []


class FakeSpawner:
    """Fake spawner that tracks image/env changes."""
    def __init__(self, user, image="jupyter/scipy-notebook:latest"):
        self.user = user
        self.image = image
        self.mem_limit = "100G"
        self.cpu_limit = 32.0
        self.mem_guarantee = "1G"
        self.cpu_guarantee = 0.25
        self.environment = {}


@pytest.mark.asyncio
async def test_spawn_hook_swaps_image_for_over_limit_user():
    cache = UsageCache(os.environ["JSU_CACHE_DB_PATH"] + "_7")
    cache.update("alice", 250, "over_limit")

    hook = make_pre_spawn_hook(cache)
    spawner = FakeSpawner(FakeUser("alice", admin=False))

    original_image = spawner.image
    await hook(spawner)

    assert spawner.image == CLEANUP_IMAGE  # Swapped!
    assert spawner.image != original_image
    assert spawner.environment["JSU_CLEANUP_MODE"] == "1"
    assert spawner.mem_limit == "256M"      # Resource limited
    assert spawner.cpu_limit == 0.5


@pytest.mark.asyncio
async def test_spawn_hook_allows_admin_normal_server():
    """Admins get their normal server even for over-limit users."""
    cache = UsageCache(os.environ["JSU_CACHE_DB_PATH"] + "_8")
    cache.update("alice", 250, "over_limit")

    hook = make_pre_spawn_hook(cache)
    original_image = "jupyter/scipy-notebook:latest"
    spawner = FakeSpawner(FakeUser("alice", admin=True), image=original_image)

    await hook(spawner)

    assert spawner.image == original_image  # NOT swapped
    assert "JSU_CLEANUP_MODE" not in spawner.environment


@pytest.mark.asyncio
async def test_spawn_hook_normal_user_under_limit():
    cache = UsageCache(os.environ["JSU_CACHE_DB_PATH"] + "_9")
    cache.update("alice", 50, "ok")

    hook = make_pre_spawn_hook(cache)
    original_image = "jupyter/scipy-notebook:latest"
    spawner = FakeSpawner(FakeUser("alice", admin=False), image=original_image)

    await hook(spawner)

    assert spawner.image == original_image  # NOT swapped


@pytest.mark.asyncio
async def test_spawn_hook_no_cache_data():
    cache = UsageCache(os.environ["JSU_CACHE_DB_PATH"] + "_10")

    hook = make_pre_spawn_hook(cache)
    original_image = "jupyter/scipy-notebook:latest"
    spawner = FakeSpawner(FakeUser("newuser", admin=False), image=original_image)

    await hook(spawner)

    assert spawner.image == original_image  # NOT swapped


@pytest.mark.asyncio
async def test_spawn_hook_excludes_service_accounts():
    os.environ["JSU_EXCLUDE_USERS"] = "grader-cs1302,all-cs1302"
    import importlib
    import jupyterhub_storage_usage.config as cfg
    importlib.reload(cfg)
    import jupyterhub_storage_usage.spawn as spawn_mod
    importlib.reload(spawn_mod)

    cache = UsageCache(os.environ["JSU_CACHE_DB_PATH"] + "_11")
    cache.update("grader-cs1302", 999, "over_limit")

    hook = spawn_mod.make_pre_spawn_hook(cache)
    original_image = "jupyter/scipy-notebook:latest"
    spawner = FakeSpawner(FakeUser("grader-cs1302", admin=False), image=original_image)

    await hook(spawner)

    assert spawner.image == original_image  # NOT swapped — excluded


@pytest.mark.asyncio
async def test_spawn_hook_blocks_without_cleanup_image():
    """If no cleanup image configured, falls back to blocking spawn."""
    os.environ.pop("JSU_CLEANUP_IMAGE", None)
    import importlib
    import jupyterhub_storage_usage.config as config_mod
    import jupyterhub_storage_usage.spawn as spawn_mod
    importlib.reload(config_mod)
    importlib.reload(spawn_mod)

    cache = UsageCache(os.environ["JSU_CACHE_DB_PATH"] + "_12")
    cache.update("alice", 250, "over_limit")

    hook = spawn_mod.make_pre_spawn_hook(cache)
    spawner = FakeSpawner(FakeUser("alice", admin=False))

    with pytest.raises(RuntimeError, match="storage limit"):
        await hook(spawner)

    # Restore for other tests
    os.environ["JSU_CLEANUP_IMAGE"] = "registry.test/cleanup:0.1"
