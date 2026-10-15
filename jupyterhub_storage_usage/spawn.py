"""Pre-spawn hook: spawns over-limit users in cleanup mode.

When a user exceeds their hard storage limit:
  1. Their normal Jupyter server is culled (by the culler)
  2. The pre-spawn hook detects they're over limit
  3. Instead of blocking, it swaps the image to the cleanup image
     and sets resource limits (low CPU/RAM)
  4. The cleanup pod runs jupyter-storage-usage in CLEANUP_MODE=1
  5. The student gets a file manager to delete/download files
  6. After cleaning up, they stop the server and spawn normally

Admins always get the normal server (to help fix issues).

Config (env vars on the hub pod):
  JSU_CLEANUP_IMAGE: the Docker image to use for cleanup pods
  JSU_CLEANUP_MEM_LIMIT: memory limit for cleanup pods (default: 256Mi)
  JSU_CLEANUP_CPU_LIMIT: CPU limit for cleanup pods (default: 0.5)
  JSU_CLEANUP_MEM_GUARANTEE: memory guarantee (default: 128Mi)
  JSU_CLEANUP_CPU_GUARANTEE: CPU guarantee (default: 0.1)
"""

from __future__ import annotations

import logging
import os
import time

from .cache import UsageCache
from .config import (
    ADMIN_CAN_SPAWN_OVER_LIMIT,
    CLEANUP_IMAGE,
    CLEANUP_CPU_GUARANTEE,
    CLEANUP_CPU_LIMIT,
    CLEANUP_MEM_GUARANTEE,
    CLEANUP_MEM_LIMIT,
    EXCLUDE_USERS,
    HARD_LIMIT_BYTES,
    HUB_API_TOKEN,
    HUB_REPORT_URL,
    WARN_LIMIT_BYTES,
)
from .limits import resolve_limits

logger = logging.getLogger("jupyterhub_storage_usage")

_GIB = 1024**3


async def _realtime_usage(spawner, username: str) -> int | None:
    """Query the user's running pod for real-time storage usage.

    If the pod is running and responds, return the fresh used_bytes.
    If the pod is not running or the query fails, return None (fall
    back to cache).
    """
    import aiohttp
    try:
        # Get the server URL from the spawner
        # JupyterHub stores the running server's URL in spawner.user.server
        server = spawner.user.spawners.get("")
        if not server or not server.latest_progress:
            # No running server
            return None

        # Build the API URL — the pod's /api/storage/v1/usage endpoint
        # The pod listens on port 8888 internally, but we can reach it
        # via the JupyterHub API: /hub/api/users/{user}/server
        # Actually, we can query the pod directly via the proxy.
        # But the simplest way is to query the K8s pod IP directly.
        import ssl
        sa_token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        with open(sa_token_path) as f:
            token = f.read().strip()
        ssl_ctx = ssl.create_default_context(cafile=ca_path)
        namespace = os.environ.get("POD_NAMESPACE", "default")
        # Get the pod's IP
        url = (
            f"https://kubernetes.default.svc/api/v1/namespaces/"
            f"{namespace}/pods/jupyter-{username}"
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                ssl=ssl_ctx,
            ) as resp:
                if resp.status != 200:
                    return None
                pod_data = await resp.json()
                pod_ip = pod_data.get("status", {}).get("podIP")
                if not pod_ip:
                    return None

            # Query the pod's storage-usage API
            api_url = f"http://{pod_ip}:8888/api/storage/v1/usage"
            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("used_bytes")
    except Exception as e:
        logger.debug("Real-time usage check failed for %s: %s", username, e)
        return None


def make_pre_spawn_hook(cache: UsageCache):
    """Return a pre_spawn_hook function for JupyterHub.

    In the new architecture, the hook does NOT block spawn.
    Instead, it detects over-limit users and swaps their spawner config
    to use the cleanup image with resource limits.

    This means over-limit users can still spawn — but they get a
    cleanup pod, not their normal JupyterLab.
    """

    async def storage_quota_hook(spawner):
        username = spawner.user.name

        # Skip excluded users (graders, service accounts)
        if username in EXCLUDE_USERS:
            return

        # Resolve per-user/group limits (overrides file is re-read each call)
        group_names = [g.name for g in spawner.user.groups] if spawner.user.groups else []
        warn_bytes, hard_bytes = resolve_limits(
            username, group_names, WARN_LIMIT_BYTES, HARD_LIMIT_BYTES,
        )

        # Inject resolved limits + report URL + auth token into the pod's env so the
        # pod-side monitor and widget show the correct per-user limits and can
        # report usage back to the hub (authenticated with the service API token).
        if hasattr(spawner, 'environment'):
            spawner.environment['JSU_WARN_LIMIT_BYTES'] = str(warn_bytes)
            spawner.environment['JSU_HARD_LIMIT_BYTES'] = str(hard_bytes)
            spawner.environment['JSU_HUB_REPORT_URL'] = HUB_REPORT_URL
            spawner.environment['JSU_HUB_API_TOKEN'] = HUB_API_TOKEN
        else:
            if not hasattr(spawner, 'extra_env'):
                spawner.extra_env = {}
            spawner.extra_env['JSU_WARN_LIMIT_BYTES'] = str(warn_bytes)
            spawner.extra_env['JSU_HARD_LIMIT_BYTES'] = str(hard_bytes)
            spawner.extra_env['JSU_HUB_REPORT_URL'] = HUB_REPORT_URL
            spawner.extra_env['JSU_HUB_API_TOKEN'] = HUB_API_TOKEN

        # Log if limits differ from global default
        if warn_bytes != WARN_LIMIT_BYTES or hard_bytes != HARD_LIMIT_BYTES:
            logger.info(
                "Custom limits for %s (groups=%s): warn=%d, hard=%d",
                username, group_names, warn_bytes, hard_bytes,
            )

        cached = cache.get(username)
        if not cached:
            # No data yet (first login, or cache expired) — allow normal spawn
            # but still record the resolved limits so the dashboard works
            cache.update(username, 0, "unknown", "", warn_bytes, hard_bytes)
            logger.debug("No cache data for %s, allowing normal spawn", username)
            return

        # Re-check status against the user's resolved limits (not global)
        used = cached["used_bytes"]
        if used <= hard_bytes:
            # Under limit — allow normal spawn
            # Update limits in cache (in case group membership changed)
            cache.update(username, used, "ok",
                         cached.get("server_url", ""), warn_bytes, hard_bytes)
            return

        # User is over their hard limit IN THE CACHE.
        # But the cache may be stale (student may have deleted files in
        # the cleanup pod).  Do a real-time check: if the user's pod is
        # currently running, query its /storage-usage API for fresh data.
        # This prevents the "deleted files but still can't spawn" trap.
        fresh_used = await _realtime_usage(spawner, username)
        if fresh_used is not None and fresh_used <= hard_bytes:
            logger.info(
                "Cache says %s is over limit (%d bytes) but real-time "
                "check shows %d bytes — under limit. Allowing normal spawn.",
                username, used, fresh_used,
            )
            # Update the cache so future spawns don't re-check
            cache.update(username, fresh_used, "ok",
                         cached.get("server_url", ""),
                         warn_bytes, hard_bytes)
            return
        if fresh_used is not None:
            used = fresh_used  # use the fresh value for logging

        # User is over their hard limit
        used_gib = used / _GIB
        limit_gib = hard_bytes / _GIB

        # Admins get their normal server (to help fix issues manually)
        if ADMIN_CAN_SPAWN_OVER_LIMIT and spawner.user.admin:
            logger.info(
                "Admin spawning over-limit user %s (%.1f GiB > %.1f GiB) — "
                "normal server (admin override)",
                username, used_gib, limit_gib,
            )
            return

        # Swap to cleanup mode
        if not CLEANUP_IMAGE:
            # No cleanup image configured — fall back to blocking
            logger.warning(
                "User %s over limit but no JSU_CLEANUP_IMAGE configured — "
                "blocking spawn", username,
            )
            raise RuntimeError(
                f"Your home directory is using {used_gib:.1f} GiB, which exceeds "
                f"the storage limit of {limit_gib:.0f} GiB.\n\n"
                f"Please contact your instructor to clean up your files."
            )

        logger.info(
            "Spawning cleanup pod for %s (storage: %.1f GiB > %.1f GiB)",
            username, used_gib, limit_gib,
        )

        # Swap the image to the cleanup image.
        # KubeSpawner applies profile_list kubespawner_override in
        # load_user_options() which runs AFTER pre_spawn_hook, so
        # setting spawner.image alone gets clobbered. We must also
        # strip the image from all profiles so nothing overrides us.
        spawner.image = CLEANUP_IMAGE

        # Strip image and resource overrides from all profiles so
        # load_user_options() can't clobber our cleanup settings
        profile_list = getattr(spawner, 'profile_list', None)
        if profile_list and not callable(profile_list):
            for profile in profile_list:
                override = profile.get('kubespawner_override', {})
                override.pop('image', None)
                override.pop('mem_limit', None)
                override.pop('cpu_limit', None)
                override.pop('mem_guarantee', None)
                override.pop('cpu_guarantee', None)
        # Also clear user_options profile so no profile is selected
        if hasattr(spawner, 'user_options'):
            spawner.user_options = {}

        # Set resource limits (low — this is just a file manager)
        spawner.mem_limit = CLEANUP_MEM_LIMIT
        spawner.cpu_limit = float(CLEANUP_CPU_LIMIT)
        spawner.mem_guarantee = CLEANUP_MEM_GUARANTEE
        spawner.cpu_guarantee = float(CLEANUP_CPU_GUARANTEE)

        # Set the cleanup mode env var so the server extension knows
        if hasattr(spawner, 'environment'):
            spawner.environment['JSU_CLEANUP_MODE'] = '1'
        else:
            # KubeSpawner uses extra_env in some versions
            if not hasattr(spawner, 'extra_env'):
                spawner.extra_env = {}
            spawner.extra_env['JSU_CLEANUP_MODE'] = '1'

        # Override the container command — cleanupnb doesn't have
        # start-singleuser.sh, so run jupyterhub-singleuser directly.
        # This ensures the jupyterhub server extension loads properly.
        spawner.cmd = ['jupyterhub-singleuser',
                        '--ServerApp.ip=0.0.0.0']

        # Keep the same storage mounts — the cleanup pod needs access
        # to the user's home directory (PVC) to browse/delete files.
        # The spawner already has the PVC mount configured.
    return storage_quota_hook
