"""Run-deadline window defaults and env overrides.

Each window bounds one phase of the run, and every model call belongs to
exactly one of them.  ``TARGET`` is the aimed-at finish.  ``CLOSEOUT`` bounds
research: past it no worker, gate repair or provider sweep may open another
call.  ``COMPOSITION`` bounds the itinerary composition that turns the admitted
catalog into the delivered itinerary — a model path, but the deliverable rather
than research, so it gets its own window instead of competing for the research
one.  ``DELIVERY_DEADLINE`` is the outer bound for projection and persistence;
the 30 seconds it holds behind ``COMPOSITION`` is what a delivered run spends
on projection and the atomic Bundle write.

Process-level defaults apply only when *building* a new
:class:`~.delivery_bundle.RunDeadlineSnapshot`.  Everything downstream —
validation, phase observation, and the budget handed to one model, provider or
finalization call — reads the seconds embedded in that snapshot, so a later env
change can neither invalidate a historical audit row nor change how much time
an in-flight run is allowed to spend against the closeout it was sealed with.
"""

from __future__ import annotations

import os

DEFAULT_TARGET_SECONDS = 5 * 60
DEFAULT_CLOSEOUT_SECONDS = 450
DEFAULT_COMPOSITION_SECONDS = 570
DEFAULT_DELIVERY_DEADLINE_SECONDS = 10 * 60
RUN_DEADLINE_POLICY_VERSION = "run_deadline.v2"


def _deadline_env(name: str, default_seconds: int) -> int:
    """Read a positive deadline override (seconds) from the environment."""

    raw = os.getenv(name)
    if raw is None:
        return default_seconds
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default_seconds
    return value if value > 0 else default_seconds


def current_target_seconds() -> int:
    return _deadline_env("STA_DEADLINE_TARGET_SECONDS", DEFAULT_TARGET_SECONDS)


def current_closeout_seconds() -> int:
    return _deadline_env("STA_DEADLINE_CLOSEOUT_SECONDS", DEFAULT_CLOSEOUT_SECONDS)


def current_composition_seconds() -> int:
    return _deadline_env("STA_DEADLINE_COMPOSITION_SECONDS", DEFAULT_COMPOSITION_SECONDS)


def current_delivery_deadline_seconds() -> int:
    return _deadline_env("STA_DEADLINE_DELIVERY_SECONDS", DEFAULT_DELIVERY_DEADLINE_SECONDS)


# Import-time aliases for call sites that import module constants.  Values reflect the
# process env at first import; builders should prefer the current_* helpers when
# constructing a fresh snapshot after env changes in tests.
TARGET_SECONDS = current_target_seconds()
CLOSEOUT_SECONDS = current_closeout_seconds()
COMPOSITION_SECONDS = current_composition_seconds()
DELIVERY_DEADLINE_SECONDS = current_delivery_deadline_seconds()
