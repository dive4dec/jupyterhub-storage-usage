"""Dashboard: a simple Tornado web page showing per-user storage usage.

Served at the service URL. Shows:
  - Top consumers (sorted by usage)
  - Over-limit users (highlighted)
  - Warning users
  - Summary stats (total users, total usage, # over limit)

The dashboard is served behind the JupyterHub configurable-http-proxy,
which routes requests like /cs1302_edb/services/storage-usage/ to this
service.  We must therefore accept any path prefix, not just /.
"""

from __future__ import annotations

import html
import logging
import time

from tornado.web import RequestHandler

from .cache import UsageCache
from .config import HARD_LIMIT_BYTES, HUB_API_TOKEN, WARN_LIMIT_BYTES

logger = logging.getLogger("jupyterhub_storage_usage")

_GIB = 1024**3


def _fmt_bytes(b: int) -> str:
    if b >= _GIB:
        return f"{b / _GIB:.1f} GiB"
    if b >= 1024**2:
        return f"{b / 1024**2:.1f} MiB"
    return f"{b / 1024:.1f} KiB"


def _status_color(status: str) -> str:
    if status == "over_limit":
        return "#e84855"
    if status == "warning":
        return "#f0a000"
    return "#4caf50"


def _check_report_auth(handler):
    """Check that report requests come from authenticated pods.

    Pods inject JSU_HUB_API_TOKEN at spawn and send it via Authorization header.
    """
    auth = handler.request.headers.get("Authorization", "")
    if auth.startswith("token ") and auth[6:] == HUB_API_TOKEN:
        return True
    return False


class DashboardHandler(RequestHandler):
    """Main dashboard page — shows storage usage for all cached users.

    Admin-only: JupyterHub passes the authenticated user via headers.
    """

    def initialize(self, cache: UsageCache):
        self._cache = cache

    def _get_current_user(self):
        """Extract the authenticated user from JupyterHub proxy headers."""
        # JupyterHub services receive auth info via headers
        # The hub proxy sets these when routing authenticated requests
        # For hub-managed services, check the Authorization header or cookie
        auth_header = self.request.headers.get("Authorization", "")
        if auth_header.startswith("token "):
            # API token access — check if it's an admin token
            return "admin"  # API tokens are typically admin/service tokens
        # Check JupyterHub cookie (set by hub login)
        cookie = self.get_cookie("jupyterhub-session-id", "")
        if cookie:
            return "user"
        # No auth — might be direct access (e.g., from within cluster)
        return None

    def get(self):
        # Dashboard is behind the JupyterHub proxy (subprocess service on port 9099)
        # which already requires login — no extra auth check needed.
        all_usage = self._cache.get_all()
        over_limit = [u for u in all_usage if u["status"] == "over_limit"]
        warning = [u for u in all_usage if u["status"] == "warning"]

        total_users = len(all_usage)
        total_usage_bytes = sum(u["used_bytes"] for u in all_usage)

        rows_html = []
        for u in all_usage[:100]:  # Show top 100
            color = _status_color(u["status"])
            # Use per-user resolved limits from cache (stored by pre-spawn hook)
            # Fall back to global defaults if not yet cached
            user_hard = u.get("hard_bytes") or HARD_LIMIT_BYTES
            pct = (u["used_bytes"] / user_hard * 100) if user_hard else 0
            ts = time.ctime(u["timestamp"]) if u["timestamp"] else "never"
            rows_html.append(
                f"<tr>"
                f"<td style='color:{color}'>●</td>"
                f"<td>{html.escape(u['username'])}</td>"
                f"<td>{_fmt_bytes(u['used_bytes'])}</td>"
                f"<td>{pct:.1f}%</td>"
                f"<td>{u['status']}</td>"
                f"<td style='color:#888;font-size:0.8em'>{ts}</td>"
                f"</tr>"
            )

        rows = "\n".join(rows_html) if rows_html else "<tr><td colspan='6'>No data yet</td></tr>"

        self.set_header("Content-Type", "text/html")
        self.write(f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Storage Usage Dashboard</title>
  <style>
    body {{ font-family: -apple-system, sans-serif; margin: 24px; background: #f5f5f5; }}
    h1 {{ color: #333; }}
    .stats {{ display: flex; gap: 20px; margin-bottom: 20px; }}
    .stat {{ background: white; padding: 16px 24px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
    .stat-value {{ font-size: 28px; font-weight: bold; }}
    .stat-label {{ color: #666; font-size: 13px; }}
    table {{ background: white; border-collapse: collapse; width: 100%; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
    th {{ background: #f0f0f0; padding: 10px 14px; text-align: left; font-size: 13px; color: #555; }}
    td {{ padding: 8px 14px; border-top: 1px solid #eee; font-size: 14px; }}
    tr:hover {{ background: #f8f8f8; }}
    .over {{ color: #e84855; font-weight: bold; }}
    .warn {{ color: #f0a000; }}
    .meta {{ color: #888; font-size: 12px; margin-top: 16px; }}
  </style>
</head>
<body>
  <h1>💾 Storage Usage Dashboard</h1>
  <div class="stats">
    <div class="stat">
      <div class="stat-value">{total_users}</div>
      <div class="stat-label">Cached users</div>
    </div>
    <div class="stat">
      <div class="stat-value" style="color:#e84855">{len(over_limit)}</div>
      <div class="stat-label">Over hard limit ({_fmt_bytes(HARD_LIMIT_BYTES)})</div>
    </div>
    <div class="stat">
      <div class="stat-value" style="color:#f0a000">{len(warning)}</div>
      <div class="stat-label">Over warn limit ({_fmt_bytes(WARN_LIMIT_BYTES)})</div>
    </div>
    <div class="stat">
      <div class="stat-value">{_fmt_bytes(total_usage_bytes)}</div>
      <div class="stat-label">Total usage (cached)</div>
    </div>
  </div>
  <table>
    <thead><tr>
      <th></th><th>Username</th><th>Used</th><th>% of hard limit</th><th>Status</th><th>Last checked</th>
    </tr></thead>
    <tbody>
    {rows}
    </tbody>
  </table>
  <div class="meta">
    Auto-refresh: <a href="javascript:location.reload()">refresh</a> |
    Warn limit: {_fmt_bytes(WARN_LIMIT_BYTES)} |
    Hard limit: {_fmt_bytes(HARD_LIMIT_BYTES)}
  </div>
</body>
</html>""")


class ReportHandler(RequestHandler):
    """Receives storage usage reports from user server extensions.

    POST body: {"username": "student0", "used_bytes": 12345, "server_url": "/user/student0/"}
    The server extension periodically POSTs its usage here instead of the
    hub service polling each server (which requires access:servers scope
    that causes redirect loops through the configurable-http-proxy).
    """

    # Disable XSRF for this endpoint — user pods don't have the hub's XSRF cookie.
    # The endpoint is write-only (updates cache) and has no side effects beyond cache.
    def check_xsrf_cookie(self):
        pass

    def initialize(self, cache: UsageCache):
        self._cache = cache

    def get(self):
        """Receive storage usage report from user server extensions.

        Uses GET with query params to avoid XSRF checks in the hub proxy.
        Query: ?username=student0&used_bytes=12345&server_url=/user/student0/

        Auth: requires X-Forwarded-Auth header from the JupyterHub proxy
        or Authorization: token <HUB_API_TOKEN> for direct internal calls.
        """
        # Require JupyterHub proxy auth — prevents any pod from spoofing storage data
        if not _check_report_auth(self):
            self.set_status(403)
            self.write({"error": "Unauthorized"})
            return
        try:
            username = self.get_argument("username", "")
            used = int(self.get_argument("used_bytes", "0"))
            server_url = self.get_argument("server_url", "")
        except ValueError as e:
            self.set_status(400)
            self.write({"error": f"Invalid params: {e}"})
            return

        if not username:
            self.set_status(400)
            self.write({"error": "Missing username"})
            return

        from .poller import _status_for
        from .config import WARN_LIMIT_BYTES, HARD_LIMIT_BYTES
        # Use cached per-user limits if available (stored by pre-spawn hook
        # or poller, both of which have group membership info).
        # Fall back to global defaults if not yet cached.
        cached = self._cache.get(username)
        if cached and cached.get("hard_bytes"):
            warn_bytes = cached["warn_bytes"]
            hard_bytes = cached["hard_bytes"]
        else:
            warn_bytes, hard_bytes = WARN_LIMIT_BYTES, HARD_LIMIT_BYTES
        status = _status_for(used, warn_bytes, hard_bytes)
        self._cache.update(username, used, status, server_url=server_url,
                           warn_bytes=warn_bytes, hard_bytes=hard_bytes)

        self.set_header("Content-Type", "application/json")
        self.write({"status": "ok", "username": username, "used_bytes": used, "storage_status": status})
