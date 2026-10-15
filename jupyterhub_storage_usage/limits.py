"""Per-user / per-group storage limit overrides.

Reads a JSON config file that maps users and groups to custom warn/hard
limits. Limits can be specified in Kubernetes-style quantity strings
(e.g. "10Gi", "500Mi", "1Ti") or plain bytes.

Resolution order (highest priority first):
  1. User-specific override
  2. Group-specific override (first matching group)
  3. Global default (from JSU_WARN_LIMIT_BYTES / JSU_HARD_LIMIT_BYTES)

Config file format (JSON):
{
  "users": {
    "ccha23": {"warn": "30Gi", "hard": "60Gi"},
    "student0": {"warn": "5Gi", "hard": "10Gi"}
  },
  "groups": {
    "project": {"warn": "20Gi", "hard": "50Gi"},
    "grader": {"warn": "50Gi", "hard": "100Gi"}
  }
}

The config file is re-read on every resolve() call, so updates take
effect immediately without restarting the hub.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from .quantity import parse_quantity

logger = logging.getLogger("jupyterhub_storage_usage")


def _load_overrides() -> dict:
    """Load the overrides JSON file. Returns empty dict if not configured."""
    # Read path from env at call time (not module import) so tests and
    # live updates work.  config.py reads it at import time, but we need
    # the flexibility to change it at runtime.
    overrides_path = os.environ.get("JSU_LIMIT_OVERRIDES_PATH", "")
    if not overrides_path:
        return {}

    try:
        with open(overrides_path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.debug("Limit overrides file not found: %s", overrides_path)
        return {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load limit overrides: %s", e)
        return {}


def _parse_limit_entry(entry: dict, context: str) -> tuple[int, int] | None:
    """Parse a single user/group limit entry.

    Returns (warn_bytes, hard_bytes) or None if entry is invalid.
    Accepts both K8s quantity strings ("10Gi") and plain ints (bytes).
    """
    if not isinstance(entry, dict):
        logger.warning("Invalid limit entry for %s: expected dict, got %s", context, type(entry))
        return None

    warn_raw = entry.get("warn")
    hard_raw = entry.get("hard")

    # Also accept "warn_gib"/"hard_gib" for backward compat (but prefer "warn"/"hard")
    if warn_raw is None:
        warn_raw = entry.get("warn_gib")
        if warn_raw is not None:
            # Convert GiB number to bytes
            warn_bytes = int(float(warn_raw) * 1024 ** 3)
        else:
            return None
    else:
        try:
            warn_bytes = parse_quantity(str(warn_raw)) if isinstance(warn_raw, str) else int(warn_raw)
        except ValueError as e:
            logger.warning("Invalid warn limit for %s: %s", context, e)
            return None

    if hard_raw is None:
        hard_raw = entry.get("hard_gib")
        if hard_raw is not None:
            hard_bytes = int(float(hard_raw) * 1024 ** 3)
        else:
            return None
    else:
        try:
            hard_bytes = parse_quantity(str(hard_raw)) if isinstance(hard_raw, str) else int(hard_raw)
        except ValueError as e:
            logger.warning("Invalid hard limit for %s: %s", context, e)
            return None

    return (warn_bytes, hard_bytes)


def resolve_limits(
    username: str,
    groups: list[str] | set[str] | None,
    default_warn: int,
    default_hard: int,
) -> tuple[int, int]:
    """Resolve effective warn/hard limits for a user.

    Priority: user-specific > group-specific > global default.
    The overrides file is re-read each call so changes take effect immediately.

    Returns (warn_bytes, hard_bytes).
    """
    overrides = _load_overrides()

    # 1. User-specific override (highest priority)
    user_entry = overrides.get("users", {}).get(username)
    if user_entry:
        result = _parse_limit_entry(user_entry, f"user {username}")
        if result:
            logger.debug(
                "User override for %s: warn=%d, hard=%d", username, result[0], result[1]
            )
            return result

    # 2. Group-specific override (first matching group)
    if groups:
        group_overrides = overrides.get("groups", {})
        for group in groups:
            if group in group_overrides:
                result = _parse_limit_entry(group_overrides[group], f"group {group}")
                if result:
                    logger.debug(
                        "Group override for %s (group %s): warn=%d, hard=%d",
                        username, group, result[0], result[1],
                    )
                    return result

    # 3. Global default
    return (default_warn, default_hard)
