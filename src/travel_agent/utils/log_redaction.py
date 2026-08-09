"""Keep provider credentials out of the log, when the credential is in the URL.

Some web APIs take their key as a query parameter and offer no header form —
amap's Directions endpoints are the case this exists for. ``httpx`` logs one
INFO line per request containing the full URL, so the moment such a request is
made the key is in ``backend.log`` in clear text, and the log is grepped,
tailed and pasted into reports.

The redaction is a filter on the ``httpx`` logger rather than a change at each
call site: there is exactly one place that formats those lines, and a rule
applied there cannot be forgotten by the next provider that keys by query
string. Any module that mints a secret-bearing URL should call
:func:`install_query_secret_redaction` at import so the filter is in place
before its first request, in every entry point (server, script, test).
"""

from __future__ import annotations

import logging
import re

# ``key`` is amap, ``ak`` is baidu, the rest are the common spellings. Anchored on
# a query boundary so a path segment that merely contains the word is untouched.
_QUERY_SECRET = re.compile(
    r"((?:\?|&)(?:key|ak|token|api_?key|access_?token|secret)=)[^&\s\"]+",
    re.IGNORECASE,
)
_REDACTED = r"\1<redacted>"

_INSTALLED_LOGGERS: set[str] = set()


def redact_query_secrets(text: str) -> str:
    """Replace the value of every credential-looking query parameter."""
    return _QUERY_SECRET.sub(_REDACTED, text)


class _QuerySecretFilter(logging.Filter):
    """Rewrite credential query values in a record's message and its args.

    ``httpx`` passes the URL as a ``%s`` arg rather than baking it into ``msg``,
    so both have to be covered. A mapping-style ``args`` is left alone: it is not
    a shape this logger produces, and rewriting it blindly would risk corrupting
    a caller's own keys.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and "=" in record.msg:
            record.msg = redact_query_secrets(record.msg)
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(
                redact_query_secrets(str(value))
                if isinstance(value, str) or type(value).__name__ == "URL"
                else value
                for value in args
            )
        return True


def install_query_secret_redaction(logger_name: str = "httpx") -> None:
    """Install the redaction on ``logger_name`` once per process."""
    if logger_name in _INSTALLED_LOGGERS:
        return
    logging.getLogger(logger_name).addFilter(_QuerySecretFilter())
    _INSTALLED_LOGGERS.add(logger_name)
