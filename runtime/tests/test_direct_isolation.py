"""Paper/live account, endpoint and grant isolation contracts (52-F01/F06)."""

from __future__ import annotations

import pytest

from bt_api_run_contract import parse_config
from bt_api_runtime.direct_backtrader import (
    DirectBacktraderSession,
    DirectCredentials,
    DirectProviderProfile,
    DirectSessionError,
    DirectWriteGrant,
)

_PROVIDER = "provider/binance_demo"
_ENDPOINT = "endpoint/binance_demo"
_ACCOUNT = "account/binance_demo"
_CREDENTIAL = "credential/binance_demo"
_SCOPE = "scope/binance_demo"


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


def _profile(**overrides):
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
        credential_fields=(
            ("public_key", "BINANCE_DEMO_API_KEY"),
            ("private_key", "BINANCE_DEMO_SECRET"),
        ),
    )
    values.update(overrides)
    return DirectProviderProfile(**values)


def _credentials() -> DirectCredentials:
    return DirectCredentials(
        {
            "BINANCE_DEMO_API_KEY": "fixture-public-key",
            "BINANCE_DEMO_SECRET": "fixture-private-secret",
        }
    )


def _grant(**overrides) -> DirectWriteGrant:
    values = dict(
        run_id="i52-isolation-run-001",
        mode="paper",
        provider_ref=_PROVIDER,
        environment="demo",
        symbols=("BTCUSDT",),
        expires_at_utc="2099-01-01T00:00:00Z",
        max_order_count=1,
        max_order_notional=150.0,
    )
    values.update(overrides)
    return DirectWriteGrant(**values)


def _session(*, plan=None, profile=None, grant=None) -> DirectBacktraderSession:
    return DirectBacktraderSession(
        plan=plan if plan is not None else _plan(),
        profile=profile if profile is not None else _profile(),
        run_id="i52-isolation-run-001",
        credentials=_credentials(),
        strategy_id="iteration52-isolation",
        grant=grant if grant is not None else _grant(),
    )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    (
        ("endpoint_ref", "endpoint/binance_prod", "plan_endpoint_mismatch"),
        ("account_ref", "account/binance_prod", "plan_account_mismatch"),
        ("credential_ref", "credential/binance_prod", "plan_credential_mismatch"),
        ("scope_ref", "scope/binance_prod", "plan_scope_mismatch"),
    ),
)
def test_profile_reference_mismatch_is_rejected_before_any_factory(field, value, code):
    with pytest.raises(DirectSessionError, match=code):
        _session(profile=_profile(**{field: value}))


def test_paper_mode_rejects_a_production_profile():
    with pytest.raises(DirectSessionError, match="paper_provider_environment_mismatch"):
        _session(profile=_profile(environment="production"))


def test_live_mode_rejects_a_demo_profile():
    with pytest.raises(DirectSessionError, match="live_provider_environment_mismatch"):
        _session(
            plan=_plan(mode="live"),
            profile=_profile(environment="demo"),
            grant=_grant(
                mode="live",
                environment="demo",
                allow_live_write=True,
            ),
        )


def test_live_mode_rejects_a_demo_bound_grant_before_start():
    session = _session(
        plan=_plan(mode="live"),
        profile=_profile(environment="production"),
        grant=_grant(mode="live", environment="demo"),
    )
    report = session.run(lambda: None, seconds=1.0)
    assert report.status == "FAILED"
    assert report.code == "grant_environment_mismatch"
    assert report.started is False


def test_live_grant_without_live_write_flag_is_refused_before_start():
    session = _session(
        plan=_plan(mode="live"),
        profile=_profile(environment="production"),
        grant=_grant(mode="live", environment="production", allow_live_write=False),
    )
    report = session.run(lambda: None, seconds=1.0)
    assert report.status == "FAILED"
    assert report.code == "live_write_not_authorized"
    assert report.started is False


def test_grant_mode_is_bound_to_the_plan():
    report = _session(grant=_grant(mode="live", environment="demo")).run(
        lambda: None, seconds=1.0
    )
    assert report.status == "FAILED"
    assert report.code == "grant_mode_mismatch"
    assert report.started is False


def test_grant_symbols_are_bound_to_the_plan():
    report = _session(grant=_grant(symbols=("ETHUSDT",))).run(lambda: None, seconds=1.0)
    assert report.status == "FAILED"
    assert report.code == "grant_symbols_mismatch"
    assert report.started is False


def test_unknown_capability_and_deferred_mode_are_rejected_by_the_parser():
    with pytest.raises(Exception) as unknown_capability:
        parse_config(
            "\n".join(
                line.replace("orders.paper.write", "orders.unknown.write")
                for line in _plan_text().splitlines()
            )
            + "\n"
        )
    assert getattr(unknown_capability.value, "code", "") == "unknown_capability"
    with pytest.raises(Exception) as deferred:
        parse_config(_plan_text().replace("mode: paper", "mode: replay", 1).replace(
            "environment: paper", "environment: replay", 1
        ))
    assert getattr(deferred.value, "code", "") == "mode_deferred"


def _plan_text() -> str:
    return "\n".join(
        [
            "schema_version: 2",
            "mode: paper",
            "environment: paper",
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
            "  - orders.paper.write",
        ]
    )
