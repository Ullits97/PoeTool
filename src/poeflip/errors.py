"""Exception types.

The guiding rule from the spec: prefer failing loudly over degrading
gracefully. Silent wrong numbers are worse than no tool, so every failure
path carries enough context (endpoint, offending payload excerpt) for the
user to see what upstream changed.
"""

from __future__ import annotations

import json
from typing import Any


class PoeFlipError(Exception):
    """Base for every error this tool raises deliberately."""


class ConfigError(PoeFlipError):
    """config.yaml is missing, malformed, or still holds a placeholder."""


class FetchError(PoeFlipError):
    """A request failed after retries, or returned an unusable status."""


class SchemaError(PoeFlipError):
    """A payload did not match the shape we parse.

    Always names the endpoint and includes a bounded excerpt of the payload
    so a renamed upstream route is diagnosable from the log alone.
    """

    def __init__(self, message: str, *, endpoint: str, payload: Any = None) -> None:
        self.endpoint = endpoint
        self.payload_excerpt = _excerpt(payload)
        full = f"{message}\n  endpoint: {endpoint}"
        if self.payload_excerpt:
            full += f"\n  payload excerpt: {self.payload_excerpt}"
        super().__init__(full)


class DirectionError(SchemaError):
    """The pay/receive orientation could not be resolved from the payload.

    Raised rather than falling back to an assumed convention: an inverted
    orientation silently flips every spread in the tool (spec 2.2).
    """


def _excerpt(payload: Any, limit: int = 600) -> str:
    if payload is None:
        return ""
    try:
        text = json.dumps(payload, default=str)
    except (TypeError, ValueError):
        text = repr(payload)
    if len(text) > limit:
        text = text[:limit] + f"... [{len(text)} chars total]"
    return text
