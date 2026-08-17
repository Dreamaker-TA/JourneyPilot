"""Run-deadline window policy: which config the windows come from, and when.

Each window bounds one phase of the run, and every model call belongs to
exactly one of them.  ``TARGET`` is the aimed-at finish.  ``CLOSEOUT`` bounds
research: past it no worker, gate repair or provider sweep may open another
call.  ``COMPOSITION`` bounds the itinerary composition that turns the admitted
catalog into the delivered itinerary — a model path, but the deliverable rather
than research, so it gets its own window instead of competing for the research
one.  ``DELIVERY_DEADLINE`` is the outer bound for projection and persistence;
the 30 seconds it holds behind ``COMPOSITION`` is what a delivered run spends
on projection and the atomic Bundle write.

The *numbers* live in one place only — ``config.RunDeadlineConfig`` — and they
apply only when *building* a new :class:`~.delivery_bundle.RunDeadlineSnapshot`.
Everything downstream (validation, phase observation, and the budget handed to
one model, provider or finalization call) reads the seconds embedded in that
snapshot, so a later config change can neither invalidate a historical audit row
nor change how much time an in-flight run is allowed to spend against the
closeout it was sealed with.
"""

from __future__ import annotations

RUN_DEADLINE_POLICY_VERSION = "run_deadline.v2"


def _windows():
    # Imported lazily: entities must not pull the config package at import time,
    # and the windows are only needed when a fresh snapshot is built.
    from ..config import get_settings

    return get_settings().run_deadline


def current_target_seconds() -> int:
    return _windows().target_seconds


def current_closeout_seconds() -> int:
    return _windows().closeout_seconds


def current_composition_seconds() -> int:
    return _windows().composition_seconds


def current_delivery_deadline_seconds() -> int:
    return _windows().delivery_seconds
