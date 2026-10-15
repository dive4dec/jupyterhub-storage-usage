"""Polls active JupyterHub servers for storage usage via the
jupyter-storage-usage server extension API.

Efficiency: uses aiohttp with a connection pool to poll up to
MAX_CONCURRENT_POLLS servers concurrently. Only polls *active* (running)
servers — typically 50-200 concurrent, not all 1000 users.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
from yarl import URL

from .cache import UsageCache
from .config import (
    HARD_LIMIT_BYTES,
    HUB_API_TOKEN,
    HUB_API_URL,
    MAX_CONCURRENT_POLLS,
    POLL_INTERVAL_SEC,
    WARN_LIMIT_BYTES,
    EXCLUDE_USERS,
)
from .limits import resolve_limits

logger = logging.getLogger("jupyterhub_storage_usage")

_GIB = 1024**3


def _status_for(used: int, warn: int, hard: int) -> str:
    """Compute status based on the user's resolved limits."""
    if used > hard:
        return "over_limit"
    elif used > warn:
        return "warning"
    return "ok"


class ServerPoller:
    """Periodically polls all active servers for storage usage."""

    def __init__(self, cache: UsageCache):
        self._cache = cache
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._running = False

    async def _get_active_servers(self) -> dict[str, dict[str, Any]]:
        """Query JupyterHub API for active servers and their URLs.

        Returns: {username: {"server_url": str, "groups": list[str]}}
        """
        if not HUB_API_TOKEN:
            logger.warning("No JUPYTERHUB_API_TOKEN — cannot query hub API")
            return {}

        headers = {"Authorization": f"token {HUB_API_TOKEN}"}
        url = f"{HUB_API_URL}/users?state=active"

        # Retry with backoff — the hub API may not be ready when the
        # service first starts (JupyterHub starts managed services before
        # the hub API is fully listening).
        max_retries = 5
        data: list[dict[str, Any]] = []
        for attempt in range(1, max_retries + 1):
            try:
                assert self._session is not None
                async with self._session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        logger.error("Hub API returned %d: %s", resp.status, await resp.text())
                        return {}
                    data = await resp.json()
                break
            except Exception as e:
                if attempt < max_retries:
                    backoff = min(2 ** attempt, 10)
                    logger.warning(
                        "Hub API unreachable (attempt %d/%d): %s — retrying in %ds",
                        attempt, max_retries, e, backoff,
                    )
                    await asyncio.sleep(backoff)
                else:
                    logger.error("Failed to query hub API after %d attempts: %s", max_retries, e)
                    return {}

        servers: dict[str, dict[str, Any]] = {}
        for user in data:
            username = user["name"]
            if username in EXCLUDE_USERS:
                continue
            if "servers" not in user:
                continue
            # Extract groups from the hub API response
            user_groups = user.get("groups", [])
            # groups may be a list of strings or a list of dicts with 'name'
            group_names = []
            for g in user_groups:
                if isinstance(g, str):
                    group_names.append(g)
                elif isinstance(g, dict):
                    group_names.append(g.get("name", ""))
            for server_name, server_info in user["servers"].items():
                if not server_info.get("ready"):
                    continue
                if server_name == "":
                    url_path = f"/user/{username}/"
                else:
                    url_path = f"/user/{username}/{server_name}/"
                servers[username] = {
                    "server_url": url_path,
                    "groups": group_names,
                }

        return servers

    async def _poll_direct(self, session: aiohttp.ClientSession, username: str, server_name: str = "") -> dict[str, Any] | None:
        """Access a user's server directly via the hub API server info.

        When the hub proxy returns auth redirects, we fall back to querying
        the hub API for the server's direct URL and access it from there.
        """
        headers = {"Authorization": f"token {HUB_API_TOKEN}"}

        # Get user info (includes servers dict with direct URLs)
        url = f"{HUB_API_URL}/users/{username}"

        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.debug("Direct poll %s: user info HTTP %d", username, resp.status)
                    return None
                user_info = await resp.json()
        except Exception as e:
            logger.debug("Direct poll %s: failed to get user info: %s", username, e)
            return None

        servers = user_info.get("servers", {})
        server_info = servers.get(server_name, {})
        if not server_info:
            logger.debug("Direct poll %s: no server '%s' in user info", username, server_name)
            return None

        # The server's direct URL from the hub state (K8s pod DNS)
        state = server_info.get("state", {})
        pod_name = state.get("pod_name", "")
        namespace = state.get("namespace", "")
        if pod_name and namespace:
            # Direct pod URL — bypasses the hub proxy entirely
            direct_url = f"http://{pod_name}.{namespace}.svc.cluster.local:8888"
            # The base_url path from the server info
            url_path = server_info.get("url", "/")
            poll_url = f"{direct_url}{url_path}api/storage/v1/usage"
        else:
            # Fallback: try the server URL (may be a relative path)
            direct_url = server_info.get("url", "")
            if not direct_url.startswith("http"):
                logger.debug("Direct poll %s: server URL is not absolute: %s", username, direct_url)
                return None
            poll_url = f"{direct_url}api/storage/v1/usage"

        try:
            # Don't send the hub API token to user pods — it's a security risk
            # (malicious pods could capture it for admin hub API access).
            # The storage usage endpoint doesn't require auth for direct pod access
            # since the pod IP is only reachable from the cluster network.
            async with session.get(
                poll_url,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.debug("Direct poll %s: HTTP %d", username, resp.status)
                    return None
                return await resp.json()
        except Exception as e:
            logger.debug("Direct poll %s: request failed: %s", username, e)
            return None

    async def _poll_one(self, session: aiohttp.ClientSession, username: str,
                        url_path: str, groups: list[str] | None = None):
        """Poll a single server's storage usage endpoint.

        Access the user's notebook server directly via the hub proxy.
        The hub proxy handles authentication via the service token.
        """
        hub_base = HUB_API_URL
        # Strip the /hub/api suffix to get the hub base URL (includes baseUrl)
        # e.g. http://localhost:8081/cs1302_edb/hub/api → http://localhost:8081/cs1302_edb
        from urllib.parse import urlparse
        parsed = urlparse(HUB_API_URL)
        path = parsed.path
        if path.endswith("/hub/api"):
            path = path[: -len("/hub/api")]
        elif path.endswith("/hub/api/"):
            path = path[: -len("/hub/api/")]
        hub_base = f"{parsed.scheme}://{parsed.netloc}{path}"
        poll_url = f"{hub_base}{url_path}api/storage/v1/usage"

        headers = {"Authorization": f"token {HUB_API_TOKEN}"}

        try:
            async with session.get(
                poll_url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=False,  # Don't follow redirects — they indicate auth issues
            ) as resp:
                if resp.status == 200:
                    data: dict[str, Any] = await resp.json()
                elif resp.status in (302, 401, 403):
                    # Auth redirect — the service token may not have access:servers scope
                    # Try accessing the server directly via the hub API's server info
                    logger.debug(
                        "Poll %s: got %d via proxy, trying direct access",
                        username, resp.status,
                    )
                    direct_data = await self._poll_direct(session, username)
                    if direct_data is None:
                        return
                    data = direct_data
                else:
                    logger.debug("Poll %s: HTTP %d", username, resp.status)
                    return
        except Exception as e:
            logger.debug("Poll %s failed: %s", username, e)
            return

        used = data.get("used_bytes", 0)

        # Resolve per-user/group limits using groups from the hub API
        # (the pod API response doesn't include groups)
        user_groups = groups or []
        warn_bytes, hard_bytes = resolve_limits(
            username, user_groups, WARN_LIMIT_BYTES, HARD_LIMIT_BYTES,
        )
        status = _status_for(used, warn_bytes, hard_bytes)

        self._cache.update(username, used, status, server_url=url_path,
                           warn_bytes=warn_bytes, hard_bytes=hard_bytes)

        if status == "over_limit":
            logger.warning(
                "User %s over hard limit: %.1f GiB > %.1f GiB (groups=%s)",
                username, used / _GIB, hard_bytes / _GIB, user_groups,
            )

    async def _poll_all(self):
        """Poll all active servers concurrently."""
        servers = await self._get_active_servers()
        if not servers:
            return

        logger.info("Polling %d active servers...", len(servers))

        # Use a semaphore to limit concurrency
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_POLLS)

        assert self._session is not None

        async def _bounded_poll(username: str, info: dict[str, Any]):
            async with semaphore:
                await self._poll_one(
                    self._session, username,
                    info["server_url"], info.get("groups"),
                )

        tasks = [_bounded_poll(u, info) for u, info in servers.items()]
        await asyncio.gather(*tasks, return_exceptions=True)

        logger.info("Poll complete: %d servers checked", len(servers))

    async def start(self):
        """Start the periodic polling loop."""
        self._running = True
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=MAX_CONCURRENT_POLLS),
        )

        async def _loop():
            while self._running:
                try:
                    await self._poll_all()
                except Exception as e:
                    logger.error("Poll loop error: %s", e)
                await asyncio.sleep(POLL_INTERVAL_SEC)

        self._task = asyncio.create_task(_loop())
        logger.info(
            "Storage usage poller started (interval=%ds, max_concurrent=%d)",
            POLL_INTERVAL_SEC, MAX_CONCURRENT_POLLS,
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
