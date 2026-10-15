"""Main entry point — starts the poller, culler, and dashboard.

The cleanup portal is NOT here — it runs inside each user's cleanup pod
via the jupyter-storage-usage server extension (JSU_CLEANUP_MODE=1).

Run as a JupyterHub managed service (started via hub.command subprocess):

  JUPYTERHUB_SERVICE_PREFIX=/{baseUrl}/services/storage-usage/ \
    python -m jupyterhub_storage_usage
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from tornado.httpserver import HTTPServer
from tornado.platform.asyncio import AsyncIOMainLoop
from tornado.web import Application

from .cache import UsageCache
from .config import CLEANUP_ENABLED, DASHBOARD_PORT
from .culler import StorageCuller
from .dashboard import DashboardHandler, ReportHandler
from .poller import ServerPoller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("jupyterhub_storage_usage")


def make_app(cache: UsageCache) -> Application:
    """Build the Tornado app: dashboard + report endpoint.

    The service runs behind the JupyterHub configurable-http-proxy, which
    routes requests with a prefix (JUPYTERHUB_SERVICE_PREFIX, e.g.
    /cs1302_edb/services/storage-usage/).  We use that prefix to construct
    precise routes instead of catch-all regexes.

    User servers POST their storage usage to /report so the hub service
    can cache it without needing to poll each server individually.
    """
    import os
    prefix = os.environ.get("JUPYTERHUB_SERVICE_PREFIX", "/").rstrip("/")
    # Ensure prefix is not empty (root mount)
    if not prefix:
        prefix = ""

    handlers = [
        (f"{prefix}/report/?", ReportHandler, {"cache": cache}),
        (f"{prefix}/?", DashboardHandler, {"cache": cache}),
        # Trailing slash redirect
        (f"{prefix}", DashboardHandler, {"cache": cache}),
    ]
    return Application(handlers, debug=False, xsrf_cookies=False)


async def main_async():
    """Start all components."""
    cache = UsageCache()

    # Start the poller (polls active servers for usage)
    poller = ServerPoller(cache)
    await poller.start()

    # Start the culler (culls over-limit servers)
    culler = StorageCuller(cache)
    await culler.start()

    # Start the dashboard
    app = make_app(cache)
    server = HTTPServer(app)
    server.listen(DASHBOARD_PORT, address="0.0.0.0")
    logger.info("Dashboard on http://0.0.0.0:%d/", DASHBOARD_PORT)
    logger.info("jupyterhub-storage-usage service started")
    if not CLEANUP_ENABLED:
        logger.warning("Cleanup portal is disabled (JSU_CLEANUP_ENABLED=0)")

    # Run forever
    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("Received shutdown signal")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    await stop_event.wait()

    # Cleanup
    logger.info("Shutting down...")
    await poller.stop()
    await culler.stop()


def main():
    """Entry point for the console script."""
    try:
        AsyncIOMainLoop().install()
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
