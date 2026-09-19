"""What the trace must NOT keep, removed on the way in.

Every tool call's arguments and result are traced — that is the point of the
trace. But an agent's bash call may export a token, a fetch may carry an
Authorization header, and a `read` of a private file returns its contents; all
of that was landing verbatim and unbounded in `events.jsonl` and in the sqlite
file the visualizer serves.

Three rules, applied to every payload before either sink sees it:

  by key      a field NAMED like a secret has its value replaced outright
  by shape    a value that LOOKS like a credential is replaced wherever it sits
  by size     strings, lists, and nesting are bounded, so one 4MB stdout does
              not become 4MB of trace

This is redaction, not a guarantee. A secret with no recognisable name and no
recognisable shape passes through — which is the argument for tracing an
evidence-file REFERENCE rather than copying a private payload into the trace at
all.
"""

from __future__ import annotations

import json
import os
import re

# ASCII on purpose. `json.dumps` escapes non-ASCII by default, so a pretty
# «redacted» lands in sqlite as \u00abredacted\u00bb — still redacted, but
# invisible to the grep-the-dump audit that is how anyone actually checks.
PLACEHOLDER = "[redacted]"
MAX_STRING = 4000        # per string value
MAX_ITEMS = 100          # per list/tuple
MAX_DEPTH = 8            # nesting levels before a value is summarised
MIN_ENV_NEEDLE = 8       # shorter env values are too common to search for

SECRET_KEY = re.compile(
    r"(?i)(pass(word|wd)?|secret|token|api[_-]?key|access[_-]?key|authoriz|"
    r"credential|cookie|bearer|private[_-]?key|client[_-]?secret|session[_-]?key|"
    r"refresh[_-]?token|signature)")

SECRET_VALUE = [
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9._\-]{12,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    re.compile(r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:eyJ[A-Za-z0-9_\-]{10,}\.){2}[A-Za-z0-9_\-]{10,}"),   # JWT
]


def env_needles() -> list[str]:
    """Values of secret-NAMED environment variables, longest first.

    The operator's own `.env` is loaded into this process, so the literal
    strings that must never be traced are already in hand. Short values are
    skipped: redacting on `TOKEN=ab` would blank every `ab` in the trace.
    """
    needles = {value for name, value in os.environ.items()
               if SECRET_KEY.search(name) and len(value or "") >= MIN_ENV_NEEDLE}
    return sorted(needles, key=len, reverse=True)


def _scrub_text(text: str, needles: list[str]) -> str:
    for needle in needles:
        if needle in text:
            text = text.replace(needle, PLACEHOLDER)
    for pattern in SECRET_VALUE:
        text = pattern.sub(PLACEHOLDER, text)
    if len(text) > MAX_STRING:
        text = f"{text[:MAX_STRING]}… [truncated, {len(text)} chars]"
    return text


def scrub_json(text: str) -> str:
    """Scrub a value that is already a JSON string, and hand back a JSON string.

    Several sinks store pre-serialised JSON (`envelopes.payload_json`,
    `gate_results.checks_json`). Parsing first keeps key-name redaction working
    on the structure; a payload that does not parse — the malformed-response
    path stores whatever the agent actually said — is scrubbed as plain text.
    """
    try:
        return json.dumps(scrub(json.loads(text)))
    except (ValueError, TypeError):
        return scrub(text)


def scrub(value, *, needles: list[str] | None = None, depth: int = 0):
    """Redact and bound one payload. Returns a new JSON-safe structure."""
    needles = env_needles() if needles is None else needles
    if depth > MAX_DEPTH:
        return f"[depth limit, {type(value).__name__}]"
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            name = str(key)
            out[name] = (PLACEHOLDER if SECRET_KEY.search(name) and item not in (None, "")
                         else scrub(item, needles=needles, depth=depth + 1))
        return out
    if isinstance(value, (list, tuple)):
        items = [scrub(v, needles=needles, depth=depth + 1) for v in value[:MAX_ITEMS]]
        if len(value) > MAX_ITEMS:
            items.append(f"… [{len(value) - MAX_ITEMS} more of {len(value)}]")
        return items
    if isinstance(value, str):
        return _scrub_text(value, needles)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _scrub_text(str(value), needles)
