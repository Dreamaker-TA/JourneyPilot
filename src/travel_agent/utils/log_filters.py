"""Log filters installed as part of the app's own logging setup.

Only one lives here so far, and it exists because a library warns once per value
rather than once per fact.
"""

from __future__ import annotations

import logging
from typing import Optional, Set, Tuple

# LangGraph's msgpack deserializer emits this warning for every value it reads
# back out of a checkpoint whose type is not on its allowlist.
CHECKPOINT_SERDE_LOGGER_NAME = "langgraph.checkpoint.serde.jsonplus"

# The message template of that one warning, matched on the template rather than
# on the formatted line so the type identity comes from the record's own args.
# Its siblings on the same logger ("Blocked deserialization of …") are different
# facts and must keep every occurrence.
_UNREGISTERED_TYPE_MESSAGE_PREFIX = "Deserializing unregistered type"


class UnregisteredCheckpointTypeDedup(logging.Filter):
    """Keep the first "unregistered type" warning per type, drop its repeats.

    One gated deep run replays the same handful of typed entities through the
    checkpointer thousands of times, so eight distinct types arrive as roughly
    220 identical WARNING lines that bury the rest of the run's log.

    Only the repeats are dropped, and only for this one message: the first
    warning per ``(module, name)`` still carries the upstream "this will be
    blocked in a future version" notice and names exactly which type would need
    registering. Nothing here silences the logger, raises its level, or touches
    any other record on it — which matters because the library's *blocked*
    deserialization warning shares this logger and is not repetitive noise.
    """

    def __init__(self) -> None:
        super().__init__()
        self._reported: Set[Tuple[str, str]] = set()

    def filter(self, record: logging.LogRecord) -> bool:
        if not str(record.msg).startswith(_UNREGISTERED_TYPE_MESSAGE_PREFIX):
            return True
        # The library logs ``(module, name, module, name)`` positionally; the
        # first two are the type's identity.  A record shaped otherwise is not
        # the event this filter knows about, so it passes through.
        args = record.args
        if not isinstance(args, tuple) or len(args) < 2:
            return True
        key = (str(args[0]), str(args[1]))
        if key in self._reported:
            return False
        self._reported.add(key)
        return True


def install_checkpoint_serde_log_dedup(
    logger: Optional[logging.Logger] = None,
) -> UnregisteredCheckpointTypeDedup:
    """Attach the de-duplicating filter to the checkpoint serde logger once."""
    target = logger or logging.getLogger(CHECKPOINT_SERDE_LOGGER_NAME)
    for existing in target.filters:
        if isinstance(existing, UnregisteredCheckpointTypeDedup):
            return existing
    installed = UnregisteredCheckpointTypeDedup()
    target.addFilter(installed)
    return installed


__all__ = [
    "CHECKPOINT_SERDE_LOGGER_NAME",
    "UnregisteredCheckpointTypeDedup",
    "install_checkpoint_serde_log_dedup",
]
