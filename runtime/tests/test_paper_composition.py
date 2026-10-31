"""Contracts for the relocated venue-agnostic paper composition helpers."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bt_api_run_contract import parse_config
from bt_api_runtime.direct_backtrader import (
    DirectBacktraderSession,
    DirectCredentials,
    DirectProviderProfile,
    DirectWriteGrant,
)
from bt_api_runtime.paper_composition import (
    PaperCompositionError,
    build_grant,
    load_credentials,
    summarize,
    write_evidence,
)

_PROVIDER = "provider/binance_demo"
_ENDPOINT = "endpoint/binance_demo"
_ACCOUNT = "account/binance_demo"
_CREDENTIAL = "credential/binance_demo"
_SCOPE = "scope/binance_demo"
_KEY_NAME = "BINANCE_DEMO_API_KEY"
_SECRET_NAME = "BINANCE_DEMO_SECRET"
_FIXED_UTC = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)
_SLUG = "052_binance_demo_roundtrip"


def _plan(*, mode: str = "paper") -> object:
    lines = [
        "schema_version: 2",
        f"mode: {mode}",
        f"environment: {mode}",
        "references:",
        "  strategy: strategy/alpha003",
        f"  provider: {_PROVIDER}",
        f"  endpoint: {_ENDPOINT}",
        f"  account: {_ACCOUNT}",
        f"  credential: {_CREDENTIAL}",
        f"  scope: {_SCOPE}",
        "  product: product/binance_usdm",
        "  data_source: data_source/binance_demo",
        "  clock: clock/exchange_utc",
        "plugins:",
        "  runtime: false",
        "strategy_parameters:",
        "  symbol: BTCUSDT",
        "requested_capabilities:",
        "  - market.read",
        "  - account.read",
        f"  - orders.{mode}.write",
    ]
    return parse_config("\n".join(lines) + "\n")


def _profile(**overrides) -> DirectProviderProfile:
    values = dict(
        provider_ref=_PROVIDER,
        endpoint_ref=_ENDPOINT,
        account_ref=_ACCOUNT,
        credential_ref=_CREDENTIAL,
        scope_ref=_SCOPE,
        sdk_route_key="BINANCE___SWAP",
        environment="demo",
        account_currency="USDT",
        symbols=("BTCUSDT",),
        credential_fields=(("public_key", _KEY_NAME), ("private_key", _SECRET_NAME)),
    )
    values.update(overrides)
    return DirectProviderProfile(**values)


# ---------------------------------------------------------------------------
# load_credentials
# ---------------------------------------------------------------------------


def test_load_credentials_parses_supported_file_forms(tmp_path: Path) -> None:
    env_file = tmp_path / "credential.env"
    env_file.write_text(
        "\n".join(
            [
                "# a comment line",
                "",
                "NOT_REQUESTED=ignored",
                "no_equals_sign_here",
                f"{_KEY_NAME} = first-value ",
                f'{_SECRET_NAME}="quoted value"',
                f"EXTRA={_SECRET_NAME}",
            ]
        ),
        encoding="utf-8",
    )
    resolved = load_credentials(env_file, (_SECRET_NAME, _KEY_NAME))
    assert resolved == {
        _SECRET_NAME: "quoted value",
        _KEY_NAME: "first-value",
    }
    # Requested-name order is preserved and unrequested variables stay unread.
    assert list(resolved) == [_SECRET_NAME, _KEY_NAME]
    assert "NOT_REQUESTED" not in resolved
    assert "ignored" not in json.dumps(resolved)


def test_load_credentials_uses_the_last_repeated_file_line(tmp_path: Path) -> None:
    env_file = tmp_path / "credential.env"
    env_file.write_text(
        f"{_KEY_NAME}=stale\n{_KEY_NAME}=current\n",
        encoding="utf-8",
    )
    assert load_credentials(env_file, (_KEY_NAME,)) == {_KEY_NAME: "current"}


def test_load_credentials_falls_back_to_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "credential.env"
    env_file.write_text(f"{_KEY_NAME}=from-file\n", encoding="utf-8")
    monkeypatch.setenv(_SECRET_NAME, "from-environment")
    assert load_credentials(env_file, (_KEY_NAME, _SECRET_NAME)) == {
        _KEY_NAME: "from-file",
        _SECRET_NAME: "from-environment",
    }


def test_load_credentials_file_value_wins_over_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "credential.env"
    env_file.write_text(f"{_KEY_NAME}=from-file\n", encoding="utf-8")
    monkeypatch.setenv(_KEY_NAME, "from-environment")
    assert load_credentials(env_file, (_KEY_NAME,)) == {_KEY_NAME: "from-file"}


def test_load_credentials_ignores_a_blank_file_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "credential.env"
    env_file.write_text(f'{_KEY_NAME}=""\n', encoding="utf-8")
    monkeypatch.delenv(_KEY_NAME, raising=False)
    with pytest.raises(PaperCompositionError) as blank:
        load_credentials(env_file, (_KEY_NAME,))
    assert blank.value.code == "missing_credential_variables"
    assert blank.value.missing == (_KEY_NAME,)

    monkeypatch.setenv(_KEY_NAME, "from-environment")
    assert load_credentials(env_file, (_KEY_NAME,)) == {_KEY_NAME: "from-environment"}


def test_load_credentials_supports_an_absent_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_KEY_NAME, raising=False)
    monkeypatch.delenv(_SECRET_NAME, raising=False)
    monkeypatch.setenv(_KEY_NAME, "env-key")
    assert load_credentials(tmp_path / "missing.env", (_KEY_NAME,)) == {_KEY_NAME: "env-key"}


def test_load_credentials_error_names_missing_variables_without_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "credential.env"
    env_file.write_text(f"{_KEY_NAME}=super-secret-value\n", encoding="utf-8")
    monkeypatch.delenv(_SECRET_NAME, raising=False)
    with pytest.raises(PaperCompositionError) as raised:
        load_credentials(env_file, (_KEY_NAME, _SECRET_NAME))

    error = raised.value
    assert error.code == "missing_credential_variables"
    assert error.missing == (_SECRET_NAME,)
    assert str(error) == "missing_credential_variables: " + _SECRET_NAME
    assert "super-secret-value" not in str(error)
    assert "super-secret-value" not in repr(error)


@pytest.mark.parametrize(
    ("names", "code"),
    (
        ((), "empty_credential_names"),
        ([], "empty_credential_names"),
        (_KEY_NAME, "invalid_credential_names"),
        (("BINANCE_DEMO_API_KEY", ""), "invalid_credential_name"),
        ((_KEY_NAME, _KEY_NAME), "duplicate_credential_name"),
        ((b"BYTES_NAME",), "invalid_credential_name"),
    ),
)
def test_load_credentials_rejects_malformed_names(tmp_path: Path, names, code) -> None:
    with pytest.raises(PaperCompositionError) as raised:
        load_credentials(tmp_path / "missing.env", names)
    assert raised.value.code == code


# ---------------------------------------------------------------------------
# build_grant
# ---------------------------------------------------------------------------


def test_build_grant_binds_plan_and_profile_identity_and_expiry() -> None:
    plan = _plan()
    profile = _profile()
    grant = build_grant(
        plan,
        profile,
        "i52-paper-run-001",
        180,
        max_order_count=2,
        max_order_notional=150.0,
        clock=lambda: _FIXED_UTC,
    )
    assert isinstance(grant, DirectWriteGrant)
    assert grant.run_id == "i52-paper-run-001"
    assert grant.mode == plan.mode.value == "paper"
    assert grant.provider_ref == profile.provider_ref
    assert grant.environment == profile.environment == "demo"
    assert grant.symbols == profile.symbols == ("BTCUSDT",)
    assert grant.expires_at_utc == "2026-10-31T12:03:00Z"
    assert grant.max_order_count == 2
    assert grant.max_order_notional == 150.0


def test_build_grant_default_clock_expires_forward_whole_seconds() -> None:
    grant = build_grant(
        _plan(),
        _profile(),
        "i52-paper-run-002",
        120,
        max_order_count=1,
        max_order_notional=100.0,
    )
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", grant.expires_at_utc)
    expiry = datetime.strptime(grant.expires_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    now = datetime.now(timezone.utc)
    assert now < expiry <= now + timedelta(seconds=120) + timedelta(seconds=1)


@pytest.mark.parametrize(
    ("count", "notional"),
    (
        ("2", "150.0"),
        (2, 150),
        (" 2 ", 150.0),
        (2.0, "150"),
    ),
)
def test_build_grant_accepts_config_shaped_limit_values(count, notional) -> None:
    grant = build_grant(
        _plan(),
        _profile(),
        "i52-paper-run-003",
        60,
        max_order_count=count,
        max_order_notional=notional,
        clock=lambda: _FIXED_UTC,
    )
    assert grant.max_order_count == 2
    assert grant.max_order_notional == 150.0


@pytest.mark.parametrize(
    ("field", "value", "code"),
    (
        ("max_order_count", 0, "invalid_grant_order_count"),
        ("max_order_count", -1, "invalid_grant_order_count"),
        ("max_order_count", "0", "invalid_grant_order_count"),
        ("max_order_count", "-2", "invalid_grant_order_count"),
        ("max_order_count", 1.5, "invalid_grant_order_count"),
        ("max_order_count", "abc", "invalid_grant_order_count"),
        ("max_order_count", "", "invalid_grant_order_count"),
        ("max_order_count", True, "invalid_grant_order_count"),
        ("max_order_count", None, "invalid_grant_order_count"),
        ("max_order_notional", 0, "invalid_grant_notional"),
        ("max_order_notional", -10.0, "invalid_grant_notional"),
        ("max_order_notional", "0.0", "invalid_grant_notional"),
        ("max_order_notional", "nan", "invalid_grant_notional"),
        ("max_order_notional", "inf", "invalid_grant_notional"),
        ("max_order_notional", [], "invalid_grant_notional"),
    ),
)
def test_build_grant_rejects_non_positive_limits(field, value, code) -> None:
    limits = {"max_order_count": 2, "max_order_notional": 150.0}
    limits[field] = value
    with pytest.raises(PaperCompositionError) as raised:
        build_grant(
            _plan(),
            _profile(),
            "i52-paper-run-004",
            60,
            clock=lambda: _FIXED_UTC,
            **limits,
        )
    assert raised.value.code == code
    assert str(raised.value) == code


@pytest.mark.parametrize("seconds", [0, -1, "0", "abc", None, True, 1.5])
def test_build_grant_rejects_a_non_positive_duration(seconds) -> None:
    with pytest.raises(PaperCompositionError) as raised:
        build_grant(
            _plan(),
            _profile(),
            "i52-paper-run-005",
            seconds,
            max_order_count=1,
            max_order_notional=100.0,
            clock=lambda: _FIXED_UTC,
        )
    assert raised.value.code == "invalid_grant_duration"


@pytest.mark.parametrize(
    "moment",
    (
        _FIXED_UTC.replace(tzinfo=None),
        _FIXED_UTC.replace(tzinfo=timezone(timedelta(hours=8))),
        "2026-10-31T12:00:00Z",
    ),
)
def test_build_grant_requires_an_aware_utc_clock(moment) -> None:
    with pytest.raises(PaperCompositionError) as raised:
        build_grant(
            _plan(),
            _profile(),
            "i52-paper-run-006",
            60,
            max_order_count=1,
            max_order_notional=100.0,
            clock=lambda: moment,
        )
    assert raised.value.code == "invalid_grant_clock"


def test_build_grant_rejects_an_unrepresentable_expiry() -> None:
    with pytest.raises(PaperCompositionError) as raised:
        build_grant(
            _plan(),
            _profile(),
            "i52-paper-run-007",
            10**12,
            max_order_count=1,
            max_order_notional=100.0,
            clock=lambda: _FIXED_UTC,
        )
    assert raised.value.code == "invalid_grant_expiry"


@pytest.mark.parametrize("mode", ("paper", "live"))
def test_build_grant_always_declares_no_live_write(mode) -> None:
    profile = _profile(environment="production") if mode == "live" else _profile()
    grant = build_grant(
        _plan(mode=mode),
        profile,
        "i52-paper-run-008",
        60,
        max_order_count=1,
        max_order_notional=100.0,
        clock=lambda: _FIXED_UTC,
    )
    assert grant.mode == mode
    assert grant.allow_live_write is False


def test_direct_session_still_refuses_a_live_plan_for_a_built_grant() -> None:
    plan = _plan(mode="live")
    profile = _profile(environment="production")
    grant = build_grant(
        plan,
        profile,
        "i52-paper-run-009",
        3600,
        max_order_count=1,
        max_order_notional=100.0,
    )
    built: list = []

    session = DirectBacktraderSession(
        plan=plan,
        profile=profile,
        run_id="i52-paper-run-009",
        credentials=DirectCredentials({_KEY_NAME: "fixture-key", _SECRET_NAME: "fixture-secret"}),
        strategy_id="iteration52-composition",
        grant=grant,
        store_factory=lambda **kwargs: built.append(kwargs) or object(),
    )
    report = session.run(lambda: None, seconds=1.0)
    assert report.status == "FAILED"
    assert report.code == "live_write_not_authorized"
    assert report.started is False
    assert built == []


# ---------------------------------------------------------------------------
# write_evidence
# ---------------------------------------------------------------------------


def test_write_evidence_creates_the_directory_and_sorted_json(tmp_path: Path) -> None:
    payload = {"status": "COMPLETED", "b": 2, "a": {"nested": [1, 2]}}
    destination = tmp_path / "runs" / "evidence"
    path = write_evidence(payload, destination, _SLUG, clock=lambda: _FIXED_UTC)

    assert path.parent == destination and destination.is_dir()
    text = path.read_text(encoding="utf-8")
    assert text == json.dumps(payload, indent=2, sort_keys=True)
    assert json.loads(text) == payload
    assert text.index('"a"') < text.index('"b"')


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        ("COMPLETED", "20261031T120000Z_052_binance_demo_roundtrip_COMPLETED.json"),
        ("BLOCKED", "20261031T120000Z_052_binance_demo_roundtrip_BLOCKED.json"),
        ("PARTIAL", "20261031T120000Z_052_binance_demo_roundtrip_PARTIAL.json"),
    ),
)
def test_write_evidence_filename_uses_the_caller_slug_and_status(
    tmp_path: Path, status, expected
) -> None:
    path = write_evidence(
        {"status": status}, tmp_path / "evidence", _SLUG, clock=lambda: _FIXED_UTC
    )
    assert path.name == expected


def test_write_evidence_default_stamp_is_utc_second_granular(tmp_path: Path) -> None:
    path = write_evidence({"status": "FAILED"}, tmp_path / "evidence", "052_okx_demo")
    assert re.fullmatch(
        r"\d{8}T\d{6}Z_052_okx_demo_FAILED\.json", path.name
    ), path.name


def test_write_evidence_never_overwrites_an_existing_file(tmp_path: Path) -> None:
    directory = tmp_path / "evidence"
    directory.mkdir()
    sentinel = directory / "20261031T120000Z_052_binance_demo_roundtrip_COMPLETED.json"
    sentinel.write_text("EARLY RECORD", encoding="utf-8")

    with pytest.raises(PaperCompositionError) as raised:
        write_evidence({"status": "COMPLETED"}, directory, _SLUG, clock=lambda: _FIXED_UTC)
    assert raised.value.code == "evidence_exists"
    assert sentinel.read_text(encoding="utf-8") == "EARLY RECORD"


def test_write_evidence_second_same_second_run_fails_and_keeps_the_first(
    tmp_path: Path
) -> None:
    directory = tmp_path / "evidence"
    first = write_evidence({"status": "BLOCKED"}, directory, _SLUG, clock=lambda: _FIXED_UTC)
    with pytest.raises(PaperCompositionError) as raised:
        write_evidence({"status": "BLOCKED"}, directory, _SLUG, clock=lambda: _FIXED_UTC)
    assert raised.value.code == "evidence_exists"
    assert json.loads(first.read_text(encoding="utf-8")) == {"status": "BLOCKED"}


@pytest.mark.parametrize(
    "slug",
    (
        "",
        "052/binance",
        "..\\052",
        "052 binance",
        "052:binance",
        "052_binance\nroundtrip",
        "ünïcode",
        "052_" + "x" * 64,
    ),
)
def test_write_evidence_rejects_an_unsafe_slug(tmp_path: Path, slug) -> None:
    with pytest.raises(PaperCompositionError) as raised:
        write_evidence({"status": "COMPLETED"}, tmp_path / "evidence", slug)
    assert raised.value.code == "unsafe_evidence_slug"
    assert list(tmp_path.rglob("*.json")) == []


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"status": ""},
        {"status": "FAILED/1"},
        {"status": 2},
        {"status": None},
        {"status": "COMPLETED "},
    ),
)
def test_write_evidence_requires_a_filename_safe_status(tmp_path: Path, payload) -> None:
    with pytest.raises(PaperCompositionError) as raised:
        write_evidence(payload, tmp_path / "evidence", _SLUG, clock=lambda: _FIXED_UTC)
    assert raised.value.code == "invalid_evidence_status"


def test_write_evidence_rejects_a_non_mapping_payload(tmp_path: Path) -> None:
    with pytest.raises(PaperCompositionError) as raised:
        write_evidence(["status", "COMPLETED"], tmp_path / "evidence", _SLUG)
    assert raised.value.code == "invalid_evidence_payload"


def test_write_evidence_serialization_failure_leaves_no_file(tmp_path: Path) -> None:
    directory = tmp_path / "evidence"
    with pytest.raises(PaperCompositionError) as raised:
        write_evidence({"status": "BLOCKED", "object": object()}, directory, _SLUG)
    assert raised.value.code == "unserializable_evidence_payload"
    assert list(directory.glob("*.json")) == []


def test_write_evidence_rejects_a_file_used_as_the_directory(tmp_path: Path) -> None:
    blocker = tmp_path / "evidence"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(PaperCompositionError) as raised:
        write_evidence({"status": "BLOCKED"}, blocker, _SLUG)
    assert raised.value.code == "invalid_evidence_directory"


def test_write_evidence_requires_an_aware_utc_clock(tmp_path: Path) -> None:
    with pytest.raises(PaperCompositionError) as raised:
        write_evidence(
            {"status": "BLOCKED"},
            tmp_path / "evidence",
            _SLUG,
            clock=lambda: _FIXED_UTC.replace(tzinfo=None),
        )
    assert raised.value.code == "invalid_evidence_clock"
    assert list(tmp_path.rglob("*.json")) == []


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------


def test_summarize_projects_the_nested_report_fields() -> None:
    payload = {
        "status": "COMPLETED",
        "reason": None,
        "venue": "BINANCE",
        "evidence_path": "runs/evidence/a.json",
        "report": {
            "run_id": "i52-run-001",
            "strategy_facts": {
                "phase": "closed",
                "cancel_requested": False,
                "open_order": {"status": "Filled"},
                "close_order": {"status": "Filled"},
            },
        },
    }
    assert summarize(payload) == {
        "status": "COMPLETED",
        "reason": None,
        "venue": "BINANCE",
        "phase": "closed",
        "open_status": "Filled",
        "close_status": "Filled",
        "cancel_requested": False,
        "run_id": "i52-run-001",
        "evidence": "runs/evidence/a.json",
    }


def test_summarize_tolerates_missing_nested_sections() -> None:
    assert summarize({"status": "BLOCKED"}) == {
        "status": "BLOCKED",
        "reason": None,
        "venue": None,
        "phase": None,
        "open_status": None,
        "close_status": None,
        "cancel_requested": None,
        "run_id": None,
        "evidence": None,
    }
    partial = summarize(
        {"status": "FAILED", "report": {"run_id": "r", "strategy_facts": {"phase": "open"}}}
    )
    assert partial["phase"] == "open"
    assert partial["run_id"] == "r"
    assert partial["open_status"] is None
    assert partial["close_status"] is None


def test_summarize_requires_a_top_level_status() -> None:
    # Relocated as-is: a summary without a status is a caller bug, not a blank row.
    with pytest.raises(KeyError):
        summarize({"reason": "config_invalid"})


# ---------------------------------------------------------------------------
# package surface
# ---------------------------------------------------------------------------


def test_paper_composition_exports_resolve_lazily_from_the_package_root() -> None:
    import bt_api_runtime

    for name in (
        "PaperCompositionError",
        "build_grant",
        "load_credentials",
        "summarize",
        "write_evidence",
    ):
        assert name in bt_api_runtime.__all__
    assert bt_api_runtime.load_credentials is load_credentials
    assert bt_api_runtime.build_grant is build_grant
    assert bt_api_runtime.write_evidence is write_evidence
    assert bt_api_runtime.summarize is summarize


def test_importing_paper_composition_loads_no_backtrader_or_contract() -> None:
    code = (
        "import sys\n"
        "import bt_api_runtime\n"
        "assert 'backtrader' not in sys.modules\n"
        "assert 'bt_api_run_contract' not in sys.modules\n"
        "import bt_api_runtime.paper_composition\n"
        "assert 'backtrader' not in sys.modules\n"
        "assert 'bt_api_run_contract' not in sys.modules\n"
        "assert 'bt_api_py' not in sys.modules\n"
        "getattr(bt_api_runtime.paper_composition, 'build_grant')\n"
        "assert 'bt_api_run_contract' not in sys.modules\n"
        "print('lazy-ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "lazy-ok"
