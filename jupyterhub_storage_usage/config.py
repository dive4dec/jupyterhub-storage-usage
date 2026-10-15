"""Configuration via environment variables.

All settings are env-var based, matching how jupyterhub-idle-culler works.
Sensible defaults are provided so the service works out-of-the-box in a
standard z2jh deployment — only JSU_CLEANUP_IMAGE must be set by the user.
"""

import os


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key, "1" if default else "0")
    return val.lower() in ("1", "true", "yes", "on")


def _env_quantity(key: str, default: int) -> int:
    """Parse an env var as a K8s-style quantity or plain bytes.

    Accepts: "10Gi", "500Mi", "1Ti", "21474836480", etc.
    Falls back to plain int, then default.
    """
    raw = os.environ.get(key, "")
    if not raw:
        return default
    # Try plain int first (fast path)
    try:
        return int(raw)
    except ValueError:
        pass
    # Try K8s quantity parsing
    try:
        from .quantity import parse_quantity
        return parse_quantity(raw)
    except (ValueError, ImportError):
        return default


# ── Limits ──────────────────────────────────────────────────────────────
# Default: 10 GiB warn / 20 GiB hard — reasonable for a teaching deployment
# with ~1000 users on shared NFS storage.
WARN_LIMIT_BYTES = _env_quantity("JSU_WARN_LIMIT_BYTES", 10 * 1024**3)
HARD_LIMIT_BYTES = _env_quantity("JSU_HARD_LIMIT_BYTES", 20 * 1024**3)

# ── Polling ─────────────────────────────────────────────────────────────
POLL_INTERVAL_SEC = _env_int("JSU_POLL_INTERVAL_SEC", 600)
MAX_CONCURRENT_POLLS = _env_int("JSU_MAX_CONCURRENT_POLLS", 50)

# ── Culling ─────────────────────────────────────────────────────────────
CULL_ON_HARD_LIMIT = _env_bool("JSU_CULL_ON_HARD_LIMIT", True)
# Default: 300s (5 min) grace period so students have time to clean up
# before their server is culled.
GRACE_PERIOD_SEC = _env_int("JSU_GRACE_PERIOD_SEC", 300)

# ── Pre-spawn ───────────────────────────────────────────────────────────
ADMIN_CAN_SPAWN_OVER_LIMIT = _env_bool("JSU_ADMIN_CAN_SPAWN_OVER_LIMIT", True)

# ── Cleanup pod ─────────────────────────────────────────────────────────
# When a user is over limit, the pre-spawn hook swaps their image to this
# and sets low resource limits. The cleanup pod runs jupyter-storage-usage
# in CLEANUP_MODE=1 (file manager only, no JupyterLab).
#
# This is the ONE required config — there is no sensible default because
# the image name depends on your registry and build.
CLEANUP_ENABLED = _env_bool("JSU_CLEANUP_ENABLED", True)
CLEANUP_IMAGE = _env_str("JSU_CLEANUP_IMAGE", "")
# KubeSpawner uses K/M/G suffixes (NOT Ki/Mi/Gi)
CLEANUP_MEM_LIMIT = _env_str("JSU_CLEANUP_MEM_LIMIT", "256M")
CLEANUP_CPU_LIMIT = _env_str("JSU_CLEANUP_CPU_LIMIT", "0.5")
CLEANUP_MEM_GUARANTEE = _env_str("JSU_CLEANUP_MEM_GUARANTEE", "128M")
CLEANUP_CPU_GUARANTEE = _env_str("JSU_CLEANUP_CPU_GUARANTEE", "0.1")

# ── Exclusions ──────────────────────────────────────────────────────────
EXCLUDE_USERS = [
    u.strip() for u in _env_str("JSU_EXCLUDE_USERS", "").split(",") if u.strip()
]

# ── Cache ───────────────────────────────────────────────────────────────
# Default: /srv/jupyterhub/storage-usage.db — on the hub's persistent volume
# in z2jh (hub PVC is mounted at /srv/jupyterhub/).  This survives hub pod
# restarts.  Using /tmp/ here would lose all cache data on restart.
CACHE_DB_PATH = _env_str("JSU_CACHE_DB_PATH", "/srv/jupyterhub/storage-usage.db")

# ── Dashboard / service port ────────────────────────────────────────────
# The service listens on this port.  Default 9099 (avoids conflicts with
# hub on 8081, proxy on 8000).
DASHBOARD_PORT = _env_int("JSU_PORT", _env_int("JSU_DASHBOARD_PORT", 9099))

# ── Hub report URL ──────────────────────────────────────────────────────
# The cleanup pod reports fresh usage to the hub after each file delete.
# Default: constructed from the hub service DNS + port + service prefix.
# In z2jh, the service prefix is /{baseUrl}/services/storage-usage/.
# We read JSU_SERVICE_PREFIX (set in hub.extraEnv) or fall back to
# JUPYTERHUB_SERVICE_PREFIX (set in the subprocess command).
_service_prefix = (
    os.environ.get("JSU_SERVICE_PREFIX")
    or os.environ.get("JUPYTERHUB_SERVICE_PREFIX")
    or "/"
)
_report_default = f"http://hub:9099{_service_prefix.rstrip('/')}/report"
HUB_REPORT_URL = _env_str("JSU_HUB_REPORT_URL", _report_default)

# ── Per-user/group limit overrides ──────────────────────────────────────
# Path to a JSON file with custom limits. See limits.py for format.
# Mounted via a ConfigMap in z2jh.  Empty string = no overrides.
LIMIT_OVERRIDES_PATH = _env_str("JSU_LIMIT_OVERRIDES_PATH", "")

# ── JupyterHub connection ───────────────────────────────────────────────
# JUPYTERHUB_API_TOKEN is injected by JupyterHub when spawning the service.
# JUPYTERHUB_API_URL should be set in the hub command/env to point at the
# hub API (e.g. http://localhost:8081/{baseUrl}/hub/api when co-located).
HUB_API_TOKEN = _env_str("JUPYTERHUB_API_TOKEN", "")
HUB_API_URL = _env_str("JUPYTERHUB_API_URL", "http://localhost:8081/hub/api")
