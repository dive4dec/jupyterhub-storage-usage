"""jupyterhub-storage-usage: JupyterHub service for storage monitoring, culling, and dashboard."""

from ._version import __version__

__all__ = ["__version__", "setup_storage_quota"]


def setup_storage_quota(c):
    """One-call setup for storage quota enforcement in JupyterHub extraConfig.

    Registers a pre-spawn hook that:
      - Resolves per-user/group storage limits
      - Injects resolved limits into the pod's environment
      - Swaps over-limit users to a cleanup pod (instead of blocking spawn)
      - Does a real-time usage check to avoid stale-cache false positives

    Usage in z2jh ``hub.extraConfig``::

        from jupyterhub_storage_usage import setup_storage_quota
        setup_storage_quota(c)

    If you already have a ``pre_spawn_hook``, use ``make_pre_spawn_hook`` directly
    and chain the hooks yourself (see README for an example).

    Parameters
    ----------
    c : traitlets.config.Config
        The JupyterHub config object (``c`` in ``extraConfig``).
    """
    from .cache import UsageCache
    from .spawn import make_pre_spawn_hook

    _cache = UsageCache()
    _hook = make_pre_spawn_hook(_cache)
    _existing = c.Spawner.pre_spawn_hook

    async def _combined_hook(spawner):
        if _existing:
            await _existing(spawner)
        await _hook(spawner)

    c.Spawner.pre_spawn_hook = _combined_hook
