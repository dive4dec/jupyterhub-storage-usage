"""Culls servers that exceed the hard storage limit.

Analogous to jupyterhub-idle-culler, but triggers on storage usage
instead of idle time.

Grace period: if GRACE_PERIOD_SEC > 0, the server is not culled immediately
when first detected over limit. It's culled only after being continuously
over limit for GRACE_PERIOD_SEC. This gives students time to clean up.
"""

from __future__ import annotations

import asyncio
import logging
import time

import os

import aiohttp

from .cache import UsageCache
from .config import (
    CLEANUP_IMAGE,
    CULL_ON_HARD_LIMIT,
    GRACE_PERIOD_SEC,
    HUB_API_TOKEN,
    HUB_API_URL,
)

_K8S_NAMESPACE = os.environ.get("POD_NAMESPACE", os.environ.get("NAMESPACE", "default"))

logger = logging.getLogger("jupyterhub_storage_usage")

_GIB = 1024**3


class StorageCuller:
    """Periodically checks for over-limit servers and culls them."""

    def __init__(self, cache: UsageCache):
        self._cache = cache
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._running = False

    async def _is_cleanup_pod_running(self, username: str) -> bool:
        """Check if the user's server is running a cleanup pod.

        Queries the K8s API via the service account token (no kubernetes
        client library needed).  Compares the pod's container image against
        the configured JSU_CLEANUP_IMAGE — if it matches, the user is
        already in cleanup mode and should not be culled.
        """
        try:
            import ssl
            sa_token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
            ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
            with open(sa_token_path) as f:
                token = f.read().strip()
            ssl_ctx = ssl.create_default_context(cafile=ca_path)
            url = (
                f"https://kubernetes.default.svc/api/v1/namespaces/"
                f"{_K8S_NAMESPACE}/pods/jupyter-{username}"
            )
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    ssl=ssl_ctx,
                ) as resp:
                    if resp.status != 200:
                        logger.debug("K8s API returned %d for pod jupyter-%s", resp.status, username)
                        return False
                    data = await resp.json()
                    for c in data.get("spec", {}).get("containers", []):
                        img = c.get("image", "")
                        # Match by exact config or by cleanup mode env var
                        if CLEANUP_IMAGE and img == CLEANUP_IMAGE:
                            return True
                        # Fallback: check for JSU_CLEANUP_MODE=1 in env
                        for ev in c.get("env", []):
                            if (ev.get("name") == "JSU_CLEANUP_MODE"
                                    and ev.get("value") == "1"):
                                return True
                    return False
        except Exception as e:
            logger.debug("Could not check cleanup pod for %s: %s", username, e)
            return False

    async def _cull_server(self, username: str, server_name: str = "") -> bool:
        """Tell JupyterHub to cull (stop) a user's server."""
        if not HUB_API_TOKEN:
            logger.warning("No JUPYTERHUB_API_TOKEN — cannot cull")
            return False

        # DELETE /hub/api/users/{username}/server (default server)
        # DELETE /hub/api/users/{username}/servers/{server_name} (named server)
        if server_name:
            url = f"{HUB_API_URL}/users/{username}/servers/{server_name}"
        else:
            url = f"{HUB_API_URL}/users/{username}/server"

        headers = {"Authorization": f"token {HUB_API_TOKEN}"}

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                assert self._session is not None
                async with self._session.delete(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status in (202, 204):
                        logger.info("Culled server for %s (storage over limit)", username)
                        return True
                    elif resp.status == 400:
                        # Server already stopped
                        logger.debug("Server for %s already stopped", username)
                        return True
                    else:
                        body = await resp.text()
                        logger.error("Cull %s failed: HTTP %d %s", username, resp.status, body[:200])
                        return False
            except Exception as e:
                if attempt < max_retries:
                    backoff = min(2 ** attempt, 10)
                    logger.warning(
                        "Cull %s: hub API unreachable (attempt %d/%d): %s — retrying in %ds",
                        username, attempt, max_retries, e, backoff,
                    )
                    await asyncio.sleep(backoff)
                else:
                    logger.error("Cull %s exception after %d attempts: %s", username, max_retries, e)
                    return False
        return False

    async def _check_and_cull(self):
        """Check cache for over-limit servers and cull those past grace period."""
        if not CULL_ON_HARD_LIMIT:
            return

        over_limit = self._cache.get_over_limit()
        if not over_limit:
            return

        now = time.time()
        for entry in over_limit:
            username = entry["username"]
            first_over = entry["first_over_limit"]

            if first_over == 0:
                # Just crossed limit — update timestamp
                # (The cache.update() call already set this)
                first_over = now

            elapsed = now - first_over
            if elapsed < GRACE_PERIOD_SEC:
                logger.info(
                    "User %s over limit for %.0fs (grace: %ds) — not culling yet",
                    username, elapsed, GRACE_PERIOD_SEC,
                )
                continue

            # Check if we recently culled this user — give them time to
            # spawn a cleanup pod (which also shows as over-limit but
            # shouldn't be culled again immediately).
            last_culled = entry.get("last_culled", 0)
            if last_culled and (now - last_culled) < 300:
                logger.info(
                    "User %s was culled %.0fs ago — skipping (cleanup pod cooldown)",
                    username, now - last_culled,
                )
                continue

            # Don't cull if the user is already running a cleanup pod.
            # The cleanup pod IS the over-limit response — culling it
            # would leave the user with no way to delete files.
            if await self._is_cleanup_pod_running(username):
                logger.info(
                    "User %s is running a cleanup pod — not culling",
                    username,
                )
                continue

            logger.warning(
                "User %s over limit for %.0fs (grace exceeded) — culling. "
                "Usage: %.1f GiB",
                username, elapsed, entry["used_bytes"] / _GIB,
            )
            culled = await self._cull_server(username)
            if culled:
                self._cache.set_culled(username, now)

    async def start(self):
        """Start the culler loop. Runs less frequently than the poller."""
        self._running = True
        self._session = aiohttp.ClientSession()

        async def _loop():
            while self._running:
                try:
                    await self._check_and_cull()
                except Exception as e:
                    logger.error("Culler loop error: %s", e)
                # Check every 60s (culler doesn't need to be as fast as poller)
                await asyncio.sleep(60)

        self._task = asyncio.create_task(_loop())
        logger.info(
            "Storage culler started (grace=%ds, cull_on_hard=%s)",
            GRACE_PERIOD_SEC, CULL_ON_HARD_LIMIT,
        )

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()
