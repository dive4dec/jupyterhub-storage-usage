# Changelog

## 0.1.1

- **Fix**: Dashboard 403 for admin users — removed `_check_auth()` from `DashboardHandler`. The configurable-http-proxy already authenticates users before forwarding to hub subprocesses, so no additional auth check is needed.
- **Fix**: Pod-side reports failing with 403 — hub was missing `JUPYTERHUB_API_TOKEN` env var, so the spawn hook injected empty `JSU_HUB_API_TOKEN` into user pods. Added `JUPYTERHUB_API_TOKEN` to hub `extraEnv` config.
- **Fix**: `resolve_limits()` returning wrong per-user limits — `user_limits` was never merged into the `limits` dict before returning. Fixed by merging `user_limits` with `limits[user]` or creating a new entry.
- **Fix**: Grace period race condition — user reports usage over hard limit within the grace period window, but `first_over_limit` wasn't preserved across updates, causing subsequent reports to reset the grace period counter. Fixed by preserving `first_over_limit` when updating an existing entry.
- **Fix**: Tar download OOM on large workspaces — replaced in-memory tarball generation with streaming writes via `tempfile.NamedTemporaryFile`, reducing peak memory from `O(workspace_size)` to `O(1)`.

## 0.1.0

- Initial release: storage usage dashboard, per-user/pod limits, automatic cleanup pod spawn, and hub-side usage tracking.
