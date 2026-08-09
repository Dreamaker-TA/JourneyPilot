"""Hard-constraint scoping helpers.

Canonical implementation lives in ``entities.constraint_applicability`` so
domain mutations do not import the services layer.
"""

from __future__ import annotations

from ..entities.constraint_applicability import *  # noqa: F403
from ..entities.constraint_applicability import (  # noqa: F401
    active_hard_constraints,
    active_hard_constraint_ids,
    bind_candidate_constraint_gate_attestations,
    build_candidate_constraint_gate_attestation,
    candidate_evaluations_fingerprint,
    candidate_facts_fingerprint,
    scoped_hard_constraint_fingerprint,
)
