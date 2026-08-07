"""Fleet model policy — allowlist enforcement at agent spawn.

HAA ruling 2026-08-04 (binding), issued after the $98/hr burn incident:

  1. Nothing that MONITORS may run above nemotron.
  2. No agent or process may use anything but a DEFINED model.
     An undefined model is not a fallback — it is a request that
     requires approval.

This module is the single choke point.  ``enforce()`` is called from
``cli_agent_setup_mixin`` where the effective model is resolved, so
every spawn path (kanban dispatch, cron, CLI, gateway) passes through
it.  There is no second door.

Policy lives in ``~/.hermes/model_policy.yaml`` so it is auditable and
editable without a code change.  A missing policy file fails OPEN with
a warning (never brick the fleet on a missing config); a malformed
entry fails CLOSED for that profile.
"""

from __future__ import annotations

import os
from typing import Any, Optional

try:
    import yaml
except Exception:  # pragma: no cover - yaml is a hard dep in practice
    yaml = None  # type: ignore[assignment]


POLICY_PATH = os.path.expanduser("~/.hermes/model_policy.yaml")

# Fallback ceiling used when a profile matches a monitor pattern but the
# policy file cannot be read.  Cheapest capable tier in the fleet.
MONITOR_CEILING = "nvidia/nemotron-3-super-120b-a12b"


class ModelPolicyViolation(RuntimeError):
    """Raised when a spawn requests a model outside the allowlist."""


_cache: dict[str, Any] = {}
_cache_mtime: float = -1.0


def _load() -> dict[str, Any]:
    """Read the policy file, memoised on mtime so edits apply live."""
    global _cache, _cache_mtime
    try:
        mtime = os.path.getmtime(POLICY_PATH)
    except OSError:
        return {}
    if mtime != _cache_mtime:
        if yaml is None:
            return {}
        try:
            with open(POLICY_PATH, "r", encoding="utf-8") as fh:
                _cache = yaml.safe_load(fh) or {}
            _cache_mtime = mtime
        except Exception:
            return {}
    return _cache


def _is_monitor(profile: str, policy: dict[str, Any]) -> bool:
    pats = policy.get("monitor_patterns") or []
    return any(p in profile for p in pats)


def check(
    profile: Optional[str],
    model: Optional[str],
) -> tuple[bool, str, Optional[str]]:
    """Return ``(allowed, reason, suggested_model)``.

    Pure and side-effect free so it can be unit tested and also driven
    from an audit script without spawning anything.
    """
    if not model:
        return True, "no model resolved; upstream default applies", None
    policy = _load()
    if not policy:
        return True, "no policy file; failing open", None
    if not policy.get("enabled", True):
        return True, "policy disabled", None

    profile = profile or "(unknown)"
    allowed = set(policy.get("allowed_models") or [])
    if not allowed:
        return True, "empty allowlist; failing open", None

    # Rule 1 — monitors are capped regardless of what they ask for.
    if _is_monitor(profile, policy):
        ceiling = policy.get("monitor_ceiling") or MONITOR_CEILING
        if model != ceiling:
            return (
                False,
                f"monitor profile '{profile}' may only run {ceiling} "
                f"(requested {model})",
                ceiling,
            )
        return True, "monitor at ceiling", None

    # Rule 2 — the model must be defined in the allowlist.
    if model not in allowed:
        return (
            False,
            f"model '{model}' is not in the fleet allowlist; "
            f"requires HAA approval (add it to {POLICY_PATH})",
            policy.get("default_model"),
        )

    # Rule 3 — restricted tiers are limited to named profiles.
    for tier_model, holders in (policy.get("restricted") or {}).items():
        if model == tier_model and profile not in set(holders or []):
            return (
                False,
                f"model '{model}' is restricted to {sorted(holders or [])}; "
                f"'{profile}' is not authorised",
                policy.get("restricted_downgrade") or policy.get("default_model"),
            )

    return True, "allowed", None


def enforce(profile: Optional[str], model: Optional[str]) -> str:
    """Enforce policy at spawn.

    Returns the model to actually use.  In ``downgrade`` mode a
    violation is corrected to the suggested model; in ``block`` mode it
    raises :class:`ModelPolicyViolation`.
    """
    ok, reason, suggested = check(profile, model)
    if ok:
        return model or ""

    policy = _load()
    mode = (policy.get("mode") or "downgrade").lower()
    msg = f"[model-policy] {reason}"

    if mode == "block":
        raise ModelPolicyViolation(msg)

    target = suggested or policy.get("default_model") or MONITOR_CEILING
    try:
        import logging

        logging.getLogger("hermes.model_policy").warning(
            "%s -> downgraded to %s", msg, target
        )
    except Exception:
        pass
    return target
