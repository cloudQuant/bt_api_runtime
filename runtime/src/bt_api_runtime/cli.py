"""Offline configuration validation and doctor commands.

These commands are diagnostics, not a run mode.  They parse the single
schema-v2 user configuration with ``bt_api_run_contract`` and never import
Backtrader, SDK services or provider modules, read credentials, or use the
network.  A passing check does not authorize a session or a write.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from bt_api_run_contract import ValidatedRunConfigV2, parse_config

_EXIT_OK = 0
_EXIT_FAILED = 1
_EXIT_BLOCKED = 2
_REQUIRED_READ_CAPABILITIES = frozenset(("market.read", "account.read"))


def _fail(code: str, message: str = "") -> dict[str, Any]:
    return {"status": "BLOCKED", "code": code, "message": message}


def _load_plan(path: Path) -> tuple[ValidatedRunConfigV2 | None, dict[str, Any] | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, _fail("config_not_found")
    except OSError:
        return None, _fail("config_unreadable")
    try:
        plan = parse_config(text)
    except Exception as exc:  # noqa: BLE001 - finite contract code only
        code = getattr(exc, "code", None)
        return None, _fail(str(code) if isinstance(code, str) else "config_invalid")
    if not isinstance(plan, ValidatedRunConfigV2):
        return None, _fail("schema_v2_required")
    return plan, None


def _summary(plan: ValidatedRunConfigV2) -> dict[str, Any]:
    references = {
        name: getattr(plan.references, name)
        for name in (
            "strategy",
            "provider",
            "endpoint",
            "account",
            "credential",
            "scope",
            "product",
            "data_source",
            "clock",
        )
    }
    parameters = {
        name: type(value).__name__ for name, value in plan.strategy_parameters
    }
    return {
        "schema_version": plan.schema_version,
        "mode": plan.mode.value,
        "environment": plan.environment.value,
        "references": references,
        "plugins": dict(plan.plugins),
        "requested_capabilities": list(plan.requested_capabilities),
        "strategy_parameters": parameters,
    }


def _checks(plan: ValidatedRunConfigV2, *, include_plugins: bool) -> list[dict[str, Any]]:
    declared = frozenset(plan.requested_capabilities)
    enabled_plugins = sorted(name for name, enabled in plan.plugins if enabled)
    checks: list[dict[str, Any]] = [
        {"name": "config_parse", "status": "PASS", "code": None},
        {"name": "mode", "status": "PASS", "code": None},
        {"name": "environment", "status": "PASS", "code": None},
        {
            "name": "read_capabilities",
            "status": "PASS" if _REQUIRED_READ_CAPABILITIES <= declared else "BLOCKED",
            "code": None
            if _REQUIRED_READ_CAPABILITIES <= declared
            else "read_capability_not_requested",
        },
        {
            "name": "write_capability",
            "status": "PASS",
            "code": None,
        },
    ]
    if include_plugins:
        checks.append(
            {
                "name": "plugins",
                "status": "PASS" if not enabled_plugins else "BLOCKED",
                "code": None if not enabled_plugins else "plugin_composition_unavailable",
                "enabled": enabled_plugins,
            }
        )
    checks.extend(
        [
            {
                "name": "topology",
                "status": "NOT_CHECKED",
                "code": "topology_not_in_schema_v2",
            },
            {
                "name": "trusted_registry",
                "status": "NOT_CHECKED",
                "code": "trusted_registry_not_supplied",
            },
            {
                "name": "credentials",
                "status": "NOT_CHECKED",
                "code": "offline_command",
            },
            {
                "name": "network",
                "status": "NOT_CHECKED",
                "code": "offline_command",
            },
        ]
    )
    return checks


def _validate(args: argparse.Namespace) -> int:
    plan, failure = _load_plan(Path(args.config))
    if failure is not None:
        print(json.dumps(failure, sort_keys=True))
        return _EXIT_BLOCKED
    summary = _summary(plan)
    checks = _checks(plan, include_plugins=False)
    blocked = [check for check in checks if check["status"] == "BLOCKED"]
    payload = {
        "status": "BLOCKED" if blocked else "PASS",
        "plan": summary,
        "checks": checks,
    }
    print(json.dumps(payload, sort_keys=True))
    return _EXIT_BLOCKED if blocked else _EXIT_OK


def _doctor(args: argparse.Namespace) -> int:
    plan, failure = _load_plan(Path(args.config))
    if failure is not None:
        print(json.dumps(failure, sort_keys=True))
        return _EXIT_BLOCKED
    checks = _checks(plan, include_plugins=True)
    blocked = [check for check in checks if check["status"] == "BLOCKED"]
    not_checked = [check["name"] for check in checks if check["status"] == "NOT_CHECKED"]
    if blocked:
        status = "BLOCKED"
    elif not_checked:
        status = "PASS_WITH_NOT_CHECKED"
    else:
        status = "PASS"
    payload = {
        "status": status,
        "plan": _summary(plan),
        "checks": checks,
        "not_checked": not_checked,
    }
    print(json.dumps(payload, sort_keys=True))
    return _EXIT_BLOCKED if blocked else _EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bt-api-runtime", description=__doc__)
    parser.add_argument("--version", action="version", version="bt-api-runtime 0.1.16")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate", help="validate the unified config offline")
    validate.add_argument("--config", required=True)
    validate.set_defaults(handler=_validate)
    doctor = subparsers.add_parser("doctor", help="offline configuration diagnostics")
    doctor.add_argument("--config", required=True)
    doctor.set_defaults(handler=_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except Exception:  # noqa: BLE001 - no traceback or sensitive payload on failure
        print(json.dumps(_fail("internal_error"), sort_keys=True))
        return _EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
