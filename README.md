# jupyterhub-storage-usage

JupyterHub service that monitors per-user storage usage, culls servers that exceed limits, provides an admin dashboard, and gives over-limit students a **cleanup portal** to self-service delete/download files.

## What it does

- Runs as a JupyterHub managed service (in the hub pod)
- **Polls** active servers for storage usage (via `jupyter-storage-usage` server extension in each pod)
- Maintains a **SQLite cache** of usage data (only polls active servers — typically 50–200, not all 1000)
- **Culls** servers that exceed the hard limit (after a configurable grace period)
- **Pre-spawn hook** swaps over-limit users to a cleanup pod (admins can always spawn normally)
- **Dashboard** shows per-user storage usage, top consumers, over-limit status
- **Cleanup portal** — a restricted web file manager that runs in the user's own pod:
  - Browse files and directories with **recursive folder sizes** (sorted by size)
  - Delete files and directories
  - Download files, or folders as **zip or tar.gz**
  - Shows the student's **actual usage vs. their limits** with a progress bar
  - **No upload, no create, no edit** — application-layer enforcement
  - **Blocks all non-cleanup routes** (`/lab/`, `/api/`, etc.) — students can't bypass it
  - Reports fresh usage to the hub **after each delete** so re-spawn works immediately

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Hub pod                                                     │
│  ┌─────────────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │ ServerPoller    │  │ Culler       │  │ Dashboard      │  │
│  │ (polls active   │  │ (stops over- │  │ + /report      │  │
│  │  servers via    │  │  limit pods  │  │  endpoint      │  │
│  │  HTTP API)      │  │  after grace)│  │                │  │
│  └────────┬────────┘  └──────┬───────┘  └───────┬────────┘  │
│           │                  │                   │           │
│           ▼                  ▼                   ▼           │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  SQLite Cache (on hub PVC)  │  Pre-spawn hook         │  │
│  └───────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
         │ poll /api/storage/v1/usage     ▲ report (after delete)
         ▼                               │
┌──────────────────┐           ┌──────────────────────┐
│  Normal user pod │           │  Cleanup pod          │
│  (divenb image)  │           │  (cleanupnb image)    │
│  jupyter-storage │           │  jupyter-storage-     │
│  -usage ext      │           │  usage in CLEANUP_MODE│
│  /api/storage/   │           │  =1 (file manager)    │
│  v1/usage        │           │  Blocks /lab/ routes  │
└──────────────────┘           └──────────────────────┘
```

The cleanup portal runs **inside the user's own pod** (not the hub pod), so heavy
file operations (`du`, `tar`, `rm` on large directories) cannot affect JupyterHub.
The pod has resource limits (e.g. 256 MiB RAM, 0.5 CPU) set by the spawner.

## How the cleanup flow works

1. Student exceeds the hard limit → their normal Jupyter server is **culled**
2. Student clicks "Start My Server" on JupyterHub
3. The **pre-spawn hook** detects they're over limit in the cache
4. Instead of blocking, it **swaps the image** to the cleanup image and sets low resource limits
5. The cleanup pod serves a **file manager** at `/cleanup/` — all other routes (`/lab/`, etc.) are blocked
6. Student browses files sorted by size, deletes large ones, downloads folders as zip
7. After each delete, the pod **reports fresh usage to the hub** — cache is updated immediately
8. Student stops the cleanup pod, clicks "Start My Server" again
9. Pre-spawn hook sees updated cache → **normal JupyterLab** spawns

## Quick start (z2jh)

### 1. Install packages

```bash
# In the hub image:
pip install jupyterhub-storage-usage

# In the singleuser image:
pip install jupyter-storage-usage
```

### 2. Build a cleanup image

A lightweight image with just `jupyter-server` + `jupyter-storage-usage`:

```dockerfile
FROM python:3.12-slim
RUN pip install jupyter-server jupyterhub jupyter-storage-usage
# No JupyterLab — the cleanup portal replaces it
```

### 3. Add to your Helm values

```yaml
hub:
  services:
    storage-usage:
      url: http://0.0.0.0:9099
      command:
        - python
        - -m
        - jupyterhub_storage_usage
      environment:
        # Required: your cleanup image
        JSU_CLEANUP_IMAGE: "registry.example.com/cleanup:0.1"
        # Required: API token for pod→hub report authentication.
        # Use the same token that JupyterHub injects into this service.
        JUPYTERHUB_API_TOKEN: "your-secret-token"
        # Optional: override defaults
        # JSU_WARN_LIMIT_BYTES: "10Gi"
        # JSU_HARD_LIMIT_BYTES: "20Gi"
        # JSU_GRACE_PERIOD_SEC: "300"

  extraConfig:
    storage-quota: |
      from jupyterhub_storage_usage import setup_storage_quota
      setup_storage_quota(c)
```

That's it — **one env var + two lines of Python**. All other settings have sensible defaults (see below).

### 4. (Optional) Per-user/group limit overrides

Create a ConfigMap with a JSON file:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: storage-limits-overrides
data:
  limits-overrides.json: |
    {
      "users": {
        "instructor1": {"warn": "50Gi", "hard": "100Gi"}
      },
      "groups": {
        "project": {"warn": "20Gi", "hard": "50Gi"}
      }
    }
```

Mount it and point the service at it:

```yaml
hub:
  extraVolumes:
    - name: storage-limits
      configMap:
        name: storage-limits-overrides
  extraVolumeMounts:
    - name: storage-limits
      mountPath: /etc/jupyter-storage-usage
  services:
    storage-usage:
      environment:
        JSU_LIMIT_OVERRIDES_PATH: "/etc/jupyter-storage-usage/limits-overrides.json"
```

Limit resolution priority: **user > group > global default**. The overrides file is re-read on every check, so changes take effect immediately without restarting the hub.

## Configuration reference

All settings are environment variables. Defaults work for a standard z2jh deployment.

### Required

| Env var | Default | Description |
|---------|---------|-------------|
| `JSU_CLEANUP_IMAGE` | *(none)* | Docker image for cleanup pods. **Must be set.** |
| `JUPYTERHUB_API_TOKEN` | *(none)* | API token injected into user pods so they can report usage to the hub. **Must be set** — use the same token JupyterHub gives this service. |

### Limits

| Env var | Default | Description |
|---------|---------|-------------|
| `JSU_WARN_LIMIT_BYTES` | `10Gi` | Soft limit — show warning in UI |
| `JSU_HARD_LIMIT_BYTES` | `20Gi` | Hard limit — cull + cleanup pod |
| `JSU_LIMIT_OVERRIDES_PATH` | *(none)* | Path to JSON file with per-user/group overrides |

Accepts K8s-style quantities: `10Gi`, `500Mi`, `1Ti`, or plain bytes.

### Polling & culling

| Env var | Default | Description |
|---------|---------|-------------|
| `JSU_POLL_INTERVAL_SEC` | `600` (10 min) | How often the hub polls active servers |
| `JSU_MAX_CONCURRENT_POLLS` | `50` | Max concurrent HTTP polls |
| `JSU_CULL_ON_HARD_LIMIT` | `1` | Set `0` to disable culling |
| `JSU_GRACE_PERIOD_SEC` | `300` (5 min) | Time over limit before cull |
| `JSU_ADMIN_CAN_SPAWN_OVER_LIMIT` | `1` | Admins get normal server even over limit |
| `JSU_EXCLUDE_USERS` | *(none)* | Comma-separated usernames to skip (e.g. `grader,service-account`) |

### Cleanup pod

| Env var | Default | Description |
|---------|---------|-------------|
| `JSU_CLEANUP_ENABLED` | `1` | Set `0` to disable cleanup (block spawn instead) |
| `JSU_CLEANUP_MEM_LIMIT` | `256M` | Memory limit (KubeSpawner format: K/M/G, NOT Ki/Mi/Gi) |
| `JSU_CLEANUP_CPU_LIMIT` | `0.5` | CPU limit |
| `JSU_CLEANUP_MEM_GUARANTEE` | `128M` | Memory guarantee |
| `JSU_CLEANUP_CPU_GUARANTEE` | `0.1` | CPU guarantee |

### Service

| Env var | Default | Description |
|---------|---------|-------------|
| `JSU_PORT` | `9099` | Port for dashboard + report endpoint |
| `JSU_CACHE_DB_PATH` | `/srv/jupyterhub/storage-usage.db` | SQLite cache (on hub PVC in z2jh) |
| `JSU_HUB_REPORT_URL` | `http://hub:9099/report` | Where cleanup pods report usage after deletes |

## Cleanup portal features

| Operation | Allowed | Notes |
|-----------|---------|-------|
| Browse directories | ✅ | Sorted by size, shows recursive folder sizes |
| Delete file | ✅ | Confirmation dialog (`?confirm=yes`) |
| Delete directory | ✅ | `shutil.rmtree`, confirmation dialog |
| Download file | ✅ | Streamed, original filename |
| Download folder as zip | ✅ | Temp file, streamed (memory-safe) |
| Download folder as tar.gz | ✅ | On-the-fly streaming |
| Upload file | ❌ | No UI for upload |
| Create file | ❌ | No UI for create |
| Edit file | ❌ | No editor |
| Access `/lab/` | ❌ | Redirected to `/cleanup/` |
| Delete home root | ❌ | Blocked |

## Security

- **JupyterHub proxy authentication**: the cleanup portal is only reachable through the hub proxy, which authenticates users via OAuth2
- **Path traversal protection**: `_resolve_safe()` uses `os.path.realpath()` + prefix check
- **User isolation**: each student can only access their own PVC mount
- **XSRF disabled for cleanup endpoints**: the plain HTML forms don't carry XSRF tokens; the hub proxy provides authentication instead
- **Catch-all route blocking**: in cleanup mode, ALL non-cleanup routes redirect to `/cleanup/` — students cannot access `/lab/`, `/api/`, `/terminals/`, etc.

## Install

```bash
pip install jupyterhub-storage-usage  # in the hub image
pip install jupyter-storage-usage     # in the singleuser image
```

## License

MIT
