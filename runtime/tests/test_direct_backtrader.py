"""Focused contracts for the config-driven direct paper/live session."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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


def _plan(
    *,
    mode: str = "paper",
    provider: str = _PROVIDER,
    capabilities: tuple[str, ...] = (
        "market.read",
        "account.read",
        "orders.paper.write",
    ),
):
    lines = [
        "schema_version: 2",
        f"mode: {mode}",
        f"environment: {mode}",
        "references:",
        "  strategy: strategy/alpha003",
        f"  provider: {provider}",
        "  endpoint: endpoint/binance_demo",
        "  account: account/binance_demo",
        "  credential: credential/binance_demo",
        "  scope: scope/binance_demo",
        "  product: product/binance_usdm",
        "  data_source: data_source/binance_demo",
        "  clock: clock/exchange_utc",
        "plugins:",
        "  runtime: false",
        "strategy_parameters:",
        "  symbol: BTCUSDT",
        "requested_capabilities:",
    ]
    lines.extend("  - " + capability for capability in capabilities)
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


def _credentials():
    return DirectCredentials(
        {
            "BINANCE_DEMO_API_KEY": "fixture-public-key",
            "BINANCE_DEMO_SECRET": "fixture-private-secret",
        }
    )


def _grant(**overrides):
    values = dict(
        run_id="i52-direct-run-001",
        mode="paper",
        provider_ref=_PROVIDER,
        environment="demo",
        symbols=("BTCUSDT",),
        expires_at_utc="2099-01-01T00:00:00Z",
        max_order_count=2,
        max_order_notional=150.0,
    )
    values.update(overrides)
    return DirectWriteGrant(**values)


class _FakeBroker:
    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def get_shutdown_state(self):
        return {"state": "stopped", "flattened": True}

    def get_execution_summary(self):
        return {"submit_calls": 0, "active_orders": 0}


class _FakeStore:
    def __init__(self, config, profile):
        self.config = config
        self.profile = profile
        self.started = 0
        self.stopped = 0
        self.is_connected = False
        self.broker = _FakeBroker()
        self.broker_kwargs = None
        self.data_kwargs = []

    def start(self):
        self.started += 1
        self.is_connected = True

    def stop(self):
        self.stopped += 1
        self.is_connected = False

    def get_environment_info(self, symbol):
        return {"environment": self.profile.environment, "symbol": symbol}

    def get_account_config(self, symbol):
        return {"can_trade": True, "position_mode": self.profile.position_mode}

    def get_order_readiness(self, symbol, quantity, position_mode=None):
        return {"ready": True, "quantity": quantity, "position_mode": position_mode}

    def getbroker(self, **kwargs):
        self.broker_kwargs = dict(kwargs)
        return self.broker

    def getdata(self, **kwargs):
        self.data_kwargs.append(dict(kwargs))
        return object()

    def get_execution_summary(self):
        return {"submit_calls": 3, "active_orders": 0}


class _FakeCerebro:
    def __init__(self):
        self.broker = None
        self.datas = []
        self.strategy = None
        self.ran = 0
        self.stop_calls = 0

    def setbroker(self, broker):
        self.broker = broker

    def adddata(self, data, name=None):
        self.datas.append((data, name))

    def addstrategy(self, factory, **params):
        self.strategy = (factory, params)

    def run(self):
        self.ran += 1
        return []

    def runstop(self):
        self.stop_calls += 1


def _session(
    plan=None,
    profile=None,
    grant=None,
    run_id="i52-direct-run-001",
    allow_live_write=False,
):
    plan = plan or _plan()
    profile = profile or _profile()
    grant = grant or _grant(
        run_id=run_id,
        mode=plan.mode.value,
        environment=profile.environment,
        allow_live_write=allow_live_write,
    )
    created = {}

    def store_factory(*, config, profile):
        created["store"] = _FakeStore(config, profile)
        return created["store"]

    cerebro = _FakeCerebro()
    session = DirectBacktraderSession(
        plan=plan,
        profile=profile,
        run_id=run_id,
        credentials=_credentials(),
        strategy_id="iteration52-direct",
        grant=grant,
        store_factory=store_factory,
        cerebro_factory=lambda: cerebro,
    )
    return session, created, cerebro


def test_direct_session_composes_real_order_lifecycle_shape():
    session, created, cerebro = _session()
    report = session.run(lambda: None, seconds=5.0)
    store = created["store"]
    assert report.status == "COMPLETED"
    assert report.code == "run_completed"
    assert report.topology == "direct"
    assert report.started is True and report.broker_started is True
    assert report.readiness == {
        "BTCUSDT": {
            "environment": "demo",
            "can_trade": True,
            "position_mode": "dual_side",
            "order_ready": True,
        }
    }
    assert store.started == 1 and store.stopped == 1
    assert store.broker.started == 1 and store.broker.stopped == 1
    assert store.broker_kwargs == {
        "position_mode": "dual_side",
        "position_sync_policy": "startup",
        "position_audit_interval": 10.0,
        "shutdown_timeout": 30.0,
        "approval_expires_at_utc": "2099-01-01T00:00:00Z",
        "approval_max_order_count": 2,
    }
    assert store.data_kwargs[0]["dataname"] == "BTCUSDT"
    assert store.data_kwargs[0]["orderbook_as_ticks"] is True
    assert store.config["market_data_only"] is False
    assert store.config["symbol_routes"] == {"BTCUSDT": "BINANCE___SWAP"}
    assert store.config["required_environments"] == {"BINANCE___SWAP": "demo"}
    assert store.config["exchange_kwargs"]["BINANCE___SWAP"]["public_key"] == (
        "fixture-public-key"
    )
    assert cerebro.ran == 1
    assert report.cleanup_steps == ("broker.stop", "store.stop")
    assert report.shutdown["store_connected"] is False
    payload = json.dumps(report.to_json(), sort_keys=True)
    assert "fixture-public-key" not in payload
    assert "fixture-private-secret" not in payload
    assert "BINANCE_DEMO" not in payload


def test_direct_session_rejects_expired_grant_before_provider_start():
    session, created, _ = _session(grant=_grant(expires_at_utc="2000-01-01T00:00:00Z"))
    report = session.run(lambda: None, seconds=5.0)
    assert report.status == "FAILED"
    assert report.code == "grant_expired"
    assert created == {}
    assert report.started is False


def test_direct_session_rejects_live_write_without_explicit_flag():
    plan = _plan(mode="live", capabilities=("market.read", "account.read", "orders.live.write"))
    profile = _profile(environment="production")
    session, created, _ = _session(plan=plan, profile=profile, allow_live_write=False)
    report = session.run(lambda: None, seconds=5.0)
    assert report.status == "FAILED"
    assert report.code == "live_write_not_authorized"
    assert created == {}


def test_direct_session_rejects_cross_environment_profile_at_construction():
    plan = _plan(mode="paper")
    with pytest.raises(DirectSessionError, match="paper_provider_environment_mismatch"):
        _session(plan=plan, profile=_profile(environment="production"))


def test_direct_session_rejects_plan_provider_mismatch():
    with pytest.raises(DirectSessionError, match="plan_provider_mismatch"):
        _session(profile=_profile(provider_ref="provider/other"))


def test_direct_session_requires_write_capability_in_plan():
    plan = _plan(capabilities=("market.read", "account.read"))
    with pytest.raises(DirectSessionError, match="write_capability_not_requested"):
        _session(plan=plan)


def test_direct_session_requires_both_read_capabilities():
    plan = _plan(capabilities=("market.read", "orders.paper.write"))
    with pytest.raises(DirectSessionError, match="read_capability_not_requested"):
        _session(plan=plan)


def test_direct_session_rejects_grant_symbol_and_run_mismatch():
    session, _, _ = _session(grant=_grant(symbols=("ETHUSDT",)))
    report = session.run(lambda: None, seconds=5.0)
    assert report.code == "grant_symbols_mismatch"
    session, _, _ = _session(grant=_grant(run_id="other-run"))
    report = session.run(lambda: None, seconds=5.0)
    assert report.code == "grant_run_id_mismatch"


def test_direct_credentials_are_redacted_and_missing_values_fail():
    credentials = _credentials()
    assert "fixture" not in repr(credentials)
    session, _, _ = _session()
    with pytest.raises(DirectSessionError, match="missing_credential_value"):
        DirectBacktraderSession(
            plan=_plan(),
            profile=_profile(),
            run_id="i52-direct-run-001",
            credentials=DirectCredentials({"BINANCE_DEMO_API_KEY": "only-key"}),
            strategy_id="iteration52-direct",
            grant=_grant(),
            store_factory=lambda **kwargs: None,
        ).store_config()


def test_runtime_root_import_is_lazy():
    code = (
        "import sys\n"
        "import bt_api_runtime\n"
        "assert 'backtrader' not in sys.modules\n"
        "assert 'bt_api_py' not in sys.modules\n"
        "assert 'bt_api_run_contract' not in sys.modules\n"
        "print(bt_api_runtime.__name__)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "bt_api_runtime"


def test_idle_pump_drives_notify_idle_while_engine_is_blocked():
    import time

    from bt_api_runtime.direct_backtrader import _IdlePump

    calls = []

    class _Strategy:
        def notify_idle(self):
            calls.append(1)

    class _Cerebro:
        runningstrats = [_Strategy()]

    pump = _IdlePump(_Cerebro())
    pump.start()
    try:
        time.sleep(1.2)
    finally:
        pump.stop()
        pump.join(2.0)
    assert calls
