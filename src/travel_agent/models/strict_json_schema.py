"""The strict structured-output subset: one judge, one rewriter.

A provider that enforces this subset rejects a schema outright — it does not
degrade it the way a lenient provider does.  So a schema that is only legal on
lenient providers is a schema this application cannot ship: the first call to a
strict provider fails, every time, with nothing delivered.

This module states the subset once.  :func:`strict_schema_violations` is the
only judge of legality, and :func:`as_strict_schema` rewrites the two things
Pydantic emits that the subset forbids while carrying no meaning the subset
cannot express:

``oneOf`` → ``anyOf``
    A discriminated union's branches each pin their discriminator to a distinct
    ``const``, so at most one branch can ever match and the two keywords select
    the same thing.  The ``discriminator`` annotation itself is a JSON-Schema
    extension the subset has no slot for; it is a hint for the *generating*
    model, and the authoritative check is the Pydantic union on the way back in,
    which is untouched.

completing ``required``
    The subset has no notion of an optional property: a property is either
    required or absent from the schema.  A Pydantic field with a default lands
    in ``properties`` but not ``required``, which the subset reads as malformed.
    Completing ``required`` asks the generating model to emit the key it would
    otherwise have omitted.

What this module deliberately does *not* do is invent an encoding for a
free-form mapping (``additionalProperties`` anything other than ``False``).  The
subset has no way to say "arbitrary keys", so such a schema cannot be legalized
here — it has to change shape where the model declares it.  :func:`as_strict_schema`
raises rather than hand the transport something the provider will reject.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

# The subset's string formats.  A format outside this set is rejected rather
# than ignored, so it counts as a violation.
PERMITTED_STRING_FORMATS = frozenset(
    {
        "date-time",
        "time",
        "date",
        "duration",
        "email",
        "hostname",
        "ipv4",
        "ipv6",
        "uuid",
    }
)

# Keywords the subset has no slot for.  ``allOf`` is included because the subset
# offers no composition keyword at all; a schema that needs it has to be flattened
# where it is declared.
_FORBIDDEN_KEYWORDS = ("oneOf", "allOf", "discriminator")


class StrictSchemaError(ValueError):
    """A schema cannot be expressed in the strict structured-output subset."""


@dataclass(frozen=True)
class StrictSchemaViolation:
    """One reason a schema is not legal in the subset."""

    path: str
    rule: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - diagnostic formatting
        return f"{self.path}: {self.rule} ({self.detail})"


def _is_object_node(node: Dict[str, Any]) -> bool:
    return node.get("type") == "object" or "properties" in node


def strict_schema_violations(schema: Any) -> Tuple[StrictSchemaViolation, ...]:
    """Return every reason ``schema`` would be rejected by a strict provider.

    An empty tuple is the only statement that a schema is shippable; callers
    must not read "no exception raised" as legality.
    """

    violations: List[StrictSchemaViolation] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            if _is_object_node(node):
                additional = node.get("additionalProperties")
                if additional is not False:
                    violations.append(
                        StrictSchemaViolation(
                            path, "additionalProperties", repr(additional)
                        )
                    )
                required = set(node.get("required", []))
                missing = [
                    name for name in node.get("properties", {}) if name not in required
                ]
                if missing:
                    violations.append(
                        StrictSchemaViolation(path, "required", ", ".join(missing))
                    )
            for key, child in node.items():
                if key in _FORBIDDEN_KEYWORDS:
                    violations.append(
                        StrictSchemaViolation(path, key, _describe(child))
                    )
                elif key == "format" and child not in PERMITTED_STRING_FORMATS:
                    violations.append(
                        StrictSchemaViolation(path, "format", str(child))
                    )
                walk(child, f"{path}/{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{path}/{index}")

    walk(schema, "#")
    return tuple(violations)


def _describe(value: Any) -> str:
    if isinstance(value, list):
        return f"{len(value)} branch(es)"
    if isinstance(value, dict):
        return ", ".join(sorted(value.get("mapping", {}))) or "mapping"
    return str(value)


def promoted_required(schema: Any) -> Dict[str, List[str]]:
    """Return the properties :func:`as_strict_schema` would add to ``required``.

    Promotion is the one rewrite that changes what the generating model must
    produce, so it is reportable on its own: a caller can pin the exact set and
    find out when a new field starts being demanded of the model.
    """

    promotions: Dict[str, List[str]] = {}

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            if _is_object_node(node):
                required = set(node.get("required", []))
                missing = [
                    name for name in node.get("properties", {}) if name not in required
                ]
                if missing:
                    promotions[path] = missing
            for key, child in node.items():
                walk(child, f"{path}/{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{path}/{index}")

    walk(schema, "#")
    return promotions


def as_strict_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``schema`` rewritten into the strict subset.

    Raises :class:`StrictSchemaError` when a violation survives the rewrite —
    a free-form mapping, an unsupported ``format``, an ``allOf``.  Those cannot
    be repaired without changing the declaring model, and emitting them anyway
    only moves the failure to the provider.
    """

    rewritten = copy.deepcopy(schema)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            branches = node.pop("oneOf", None)
            if branches is not None:
                node["anyOf"] = branches
            node.pop("discriminator", None)
            if _is_object_node(node):
                node["required"] = list(node.get("properties", {}))
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(rewritten)
    remaining = strict_schema_violations(rewritten)
    if remaining:
        raise StrictSchemaError(
            "schema cannot be expressed in the strict structured-output subset: "
            + "; ".join(str(violation) for violation in remaining)
        )
    return rewritten
