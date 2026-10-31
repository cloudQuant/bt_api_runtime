"""Venue-agnostic paper composition helpers relocated from example 052.

Governance revision R5 of iteration 52 requires shared, venue-neutral
composition and evidence *mechanisms* to live in their owning layer, so the
direct paper examples keep only strategy and configuration mapping.  This
module holds those mechanisms: credential-variable loading, one bounded write
grant, evidence JSON publication and the compact stdout summary.

Nothing here opens a network connection, reads a private configuration or
authorizes a write.  Credential values are returned to the caller and never
appear in an error message; failures carry finite codes and, for missing
variables, variable *names* only.

Only the standard library is imported at module scope.  ``DirectWriteGrant`` is
imported inside :func:`build_grant` on purpose: that keeps importing this module
from pulling in ``bt_api_run_contract`` or Backtrader, which is what preserves
the lazy ``bt_api_runtime`` package root.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "PaperCompositionError",
    "build_grant",
    "load_credentials",
    "summarize",
    "write_evidence",
]

_TOKEN_MAX = 64
_SAFE_TOKEN_EXTRA = "-_"
_GRANT_EXPIRY_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_EVIDENCE_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_UTC_ZERO_OFFSET = timedelta(0)


class PaperCompositionError(RuntimeError):
    """Finite, value-free boundary error from paper composition helpers."""

    def __init__(self, code: str, *, names: tuple[str, ...] = ()) -> None:
        if type(code) is not str or not code.isascii() or not code:
            raise ValueError("code must be a non-empty ASCII string")
        self.code = code
        self.missing = tuple(names)
        super().__init__(code if not names else code + ": " + ", ".join(names))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_moment(clock: Callable[[], datetime] | None, code: str) -> datetime:
    """Require an aware, UTC-offset clock so no local time is labelled ``Z``."""

    moment = (clock or _utc_now)()
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise PaperCompositionError(code)
    offset = moment.utcoffset()
    if offset is None or offset != _UTC_ZERO_OFFSET:
        raise PaperCompositionError(code)
    return moment.replace(tzinfo=timezone.utc)


def _require_token(value: object, code: str) -> str:
    """Return a value safe to interpolate into one file name component."""

    if type(value) is not str or not value or len(value) > _TOKEN_MAX:
        raise PaperCompositionError(code)
    for char in value:
        if not (char.isascii() and (char.isalnum() or char in _SAFE_TOKEN_EXTRA)):
            raise PaperCompositionError(code)
    return value


def _positive_count(value: object, code: str) -> int:
    if isinstance(value, bool):
        raise PaperCompositionError(code)
    if type(value) is int:
        if value <= 0:
            raise PaperCompositionError(code)
        return value
    if type(value) is float:
        if not math.isfinite(value) or value <= 0 or not value.is_integer():
            raise PaperCompositionError(code)
        return int(value)
    if type(value) is str:
        text = value.strip()
        try:
            return _positive_count(int(text), code)
        except ValueError:
            pass
        try:
            return _positive_count(float(text), code)
        except ValueError:
            raise PaperCompositionError(code) from None
    raise PaperCompositionError(code)


def _positive_amount(value: object, code: str) -> float:
    if isinstance(value, bool):
        raise PaperCompositionError(code)
    if type(value) is int:
        number = float(value)
    elif type(value) is float:
        number = value
    elif type(value) is str:
        try:
            number = float(value.strip())
        except ValueError:
            raise PaperCompositionError(code) from None
    else:
        raise PaperCompositionError(code)
    if not math.isfinite(number) or number <= 0:
        raise PaperCompositionError(code)
    return number


def load_credentials(path: str | os.PathLike[str], names: Any) -> dict[str, str]:
    """Read the named variables from ``path``, then the environment; require all.

    File lines are ``NAME=value``; blank lines, ``#`` comments and lines without
    ``=`` are ignored, and one pair of surrounding quotes is stripped.  A name
    absent (or blank) in the file falls back to ``os.environ``.  The raised error
    names the missing variables and never their values.  ``names`` must be a
    non-empty tuple or list: a bare string is rejected instead of being iterated
    one character at a time.
    """

    if not isinstance(names, (tuple, list)):
        raise PaperCompositionError("invalid_credential_names")
    if not names:
        raise PaperCompositionError("empty_credential_names")
    requested = tuple(names)
    if any(type(name) is not str or not name for name in requested):
        raise PaperCompositionError("invalid_credential_name")
    if len(set(requested)) != len(requested):
        raise PaperCompositionError("duplicate_credential_name")

    source = Path(path)
    values: dict[str, str] = {}
    if source.is_file():
        for line in source.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            if key in requested:
                values[key] = value.strip().strip("'\"")
    for name in requested:
        if not values.get(name):
            values[name] = os.environ.get(name, "")
    missing = tuple(name for name in requested if not values.get(name))
    if missing:
        raise PaperCompositionError("missing_credential_variables", names=missing)
    return {name: values[name] for name in requested}


def build_grant(
    plan: Any,
    profile: Any,
    run_id: str,
    seconds: Any,
    *,
    max_order_count: Any,
    max_order_notional: Any,
    clock: Callable[[], datetime] | None = None,
) -> Any:
    """Build one bounded, one-shot write grant for the selected provider profile.

    The two numeric limits are explicit required arguments rather than
    example-specific parameter names.  Values may arrive as strings from a
    validated configuration; they are parsed and must be positive.
    """

    from bt_api_runtime.direct_backtrader import DirectWriteGrant

    duration = _positive_count(seconds, "invalid_grant_duration")
    order_count = _positive_count(max_order_count, "invalid_grant_order_count")
    notional = _positive_amount(max_order_notional, "invalid_grant_notional")
    moment = _utc_moment(clock, "invalid_grant_clock")
    try:
        expiry = moment + timedelta(seconds=duration)
    except (OverflowError, OSError, ValueError):
        raise PaperCompositionError("invalid_grant_expiry") from None

    return DirectWriteGrant(
        run_id=run_id,
        mode=plan.mode.value,
        provider_ref=profile.provider_ref,
        environment=profile.environment,
        symbols=profile.symbols,
        expires_at_utc=expiry.strftime(_GRANT_EXPIRY_FORMAT),
        max_order_count=order_count,
        max_order_notional=notional,
        # Live-write authority is never minted here.  A paper ceiling is the
        # maximum this helper can express; the direct session still refuses a
        # LIVE plan (``live_write_not_authorized``), and production permission
        # must come from the deployment's own operator boundary.
        allow_live_write=False,
    )


def write_evidence(
    payload: Mapping[str, Any],
    directory: str | os.PathLike[str],
    slug: str,
    *,
    clock: Callable[[], datetime] | None = None,
) -> Path:
    """Write one indented, sorted-keys evidence JSON and return its path.

    ``directory`` is created when absent and ``slug`` is a caller-supplied,
    filename-safe name prefix; the payload ``status`` becomes the trailing name
    component.  An existing target name is never overwritten: evidence is
    append-only history, so a same-second collision fails loudly instead of
    destroying the earlier record.
    """

    if not isinstance(payload, Mapping):
        raise PaperCompositionError("invalid_evidence_payload")
    name = _require_token(slug, "unsafe_evidence_slug")
    status = _require_token(payload.get("status"), "invalid_evidence_status")
    target_dir = Path(directory)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise PaperCompositionError("invalid_evidence_directory") from None
    stamp = _utc_moment(clock, "invalid_evidence_clock").strftime(_EVIDENCE_STAMP_FORMAT)
    path = target_dir / f"{stamp}_{name}_{status}.json"
    try:
        text = json.dumps(payload, indent=2, sort_keys=True)
    except (TypeError, ValueError) as exc:
        # Serialize before the exclusive open so a bad payload cannot leave an
        # empty or half-written evidence artifact behind.
        raise PaperCompositionError("unserializable_evidence_payload") from exc
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError:
        raise PaperCompositionError("evidence_exists") from None
    return path


def summarize(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The compact one-line stdout summary printed for every outcome."""

    report = payload.get("report") or {}
    facts = report.get("strategy_facts") or {}
    return {
        "status": payload["status"],
        "reason": payload.get("reason"),
        "venue": payload.get("venue"),
        "phase": facts.get("phase"),
        "open_status": (facts.get("open_order") or {}).get("status"),
        "close_status": (facts.get("close_order") or {}).get("status"),
        "cancel_requested": facts.get("cancel_requested"),
        "run_id": report.get("run_id"),
        "evidence": payload.get("evidence_path"),
    }
