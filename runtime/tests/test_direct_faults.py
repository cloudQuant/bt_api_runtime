"""Lifecycle, shutdown and recovery fault contracts for the direct session.

Covers iteration 52 acceptance items F03 (lifecycle/resources), F09 (uncertain
dispatch), F11 (crash/recovery boundaries) and F13 (operations/shutdown) at the
SOURCE tier: the real ``DirectBacktraderSession`` plus fake Store/Broker/Cerebro
leaves.  No provider, network, account or credential is involved.
"""

from __future__ import annotations

import threading
import time
import types

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
_RUN_ID = "i52-fault-run-001"


def _plan(*, mode: str = "paper"):
    lines = [
        "schema_version: 2",
        f"mode: {mode}",
        f"environment: {mode}",
        "references:",
        "  strategy: strategy/alpha003",
        f"  provider: {_PROVIDER}",
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


def _credentials():
    return DirectCredentials(
        {
            "BINANCE_DEMO_API_KEY": "fixture-public-key",
            "BINANCE_DEMO_SECRET": "fixture-private-secret",
        }
    )


def _grant(**overrides):
    values = dict(
        run_id=_RUN_ID,
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


class _Events(list):
    def record(self, item: str) -> None:
        self.append(item)


class _FaultStore:
    """Fake Store leaf with observable lifecycle and failure injection."""

    def __init__(
        self,
        events: _Events,
        profile: DirectProviderProfile,
        *,
        start_error: BaseException | None = None,
        stop_error: BaseException | None = None,
        reconcile_error: BaseException | None = None,
        execution_summary: dict | None = None,
    ) -> None:
        self.events = events
        self.profile = profile
        self.start_error = start_error
        self.stop_error = stop_error
        self.reconcile_error = reconcile_error
        self.start_attempts = 0
        self.started = 0
        self.stop_attempts = 0
        self.stopped = 0
        self.submit_calls = 0
        self.cancel_calls = 0
        self.is_connected = False
        self.data_kwargs: list[dict] = []
        self._api = None
        self.execution_summary = execution_summary or {
            "submit_calls": 0,
            "cancel_calls": 0,
            "active_orders": 0,
            "unknown_ids": [],
        }

    def start(self):
        self.events.record("store.start")
        self.start_attempts += 1
        if self.start_error is not None:
            raise self.start_error
        self.started += 1
        self.is_connected = True

    def stop(self):
        self.events.record("store.stop")
        self.stop_attempts += 1
        if self.stop_error is not None:
            raise self.stop_error
        self.stopped += 1
        self.is_connected = False

    def get_environment_info(self, symbol):
        return {"environment": self.profile.environment, "symbol": symbol}

    def get_account_config(self, symbol):
        return {"can_trade": True, "position_mode": self.profile.position_mode}

    def get_order_readiness(self, symbol, quantity, position_mode=None):
        return {"ready": True, "quantity": quantity, "position_mode": position_mode}

    def get_instrument_spec(self, symbol):
        return {"multiplier": 1.0, "tick_size": "0.10"}

    def get_reconcile_snapshot(self):
        if self.reconcile_error is not None:
            raise self.reconcile_error
        return {
            "positions": [],
            "open_orders": [],
            "execution_summary": {"unknown_ids": [], "active_orders": 0},
        }

    def getdata(self, **kwargs):
        self.data_kwargs.append(dict(kwargs))
        return object()

    def get_execution_summary(self):
        return dict(self.execution_summary)


class _FaultBroker:
    """Fake Broker leaf with observable start/stop ordering."""

    def __init__(
        self,
        events: _Events,
        *,
        start_error: BaseException | None = None,
        stop_error: BaseException | None = None,
        orders: dict | None = None,
    ) -> None:
        self.events = events
        self.start_error = start_error
        self.stop_error = stop_error
        self.orders = {} if orders is None else orders
        self.start_attempts = 0
        self.started = 0
        self.stop_attempts = 0
        self.stopped = 0

    def start(self):
        self.events.record("broker.start")
        self.start_attempts += 1
        if self.start_error is not None:
            raise self.start_error
        self.started += 1

    def stop(self):
        self.events.record("broker.stop")
        self.stop_attempts += 1
        if self.stop_error is not None:
            raise self.stop_error
        self.stopped += 1

    def get_shutdown_state(self):
        return {
            "state": "stopped" if self.stopped else "not_stopped",
            "flattened": bool(self.stopped),
        }

    def get_execution_summary(self):
        return {"submit_calls": 0, "cancel_calls": 0, "active_orders": 0}


class _FaultCerebro:
    """Fake Cerebro leaf; ``run`` can fail or block until ``runstop``."""

    def __init__(self, events: _Events, *, run_error: BaseException | None = None) -> None:
        self.events = events
        self.run_error = run_error
        self.broker = None
        self.datas: list = []
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
        self.events.record("cerebro.run")
        self.ran += 1
        if self.run_error is not None:
            raise self.run_error
        return []

    def runstop(self):
        self.stop_calls += 1


class _BlockingCerebro(_FaultCerebro):
    """Cerebro whose engine only returns once the deadline calls ``runstop``."""

    def __init__(self, events: _Events) -> None:
        super().__init__(events)
        self.stop_requested = threading.Event()

    def run(self):
        self.events.record("cerebro.run")
        self.ran += 1
        guard = time.monotonic() + 5.0
        while not self.stop_requested.is_set() and time.monotonic() < guard:
            time.sleep(0.01)
        return []

    def runstop(self):
        self.stop_calls += 1
        self.stop_requested.set()


class _FakeExecutionJournal:
    """Minimal execution-session shape used by the SDK recovery helper."""

    def __init__(self) -> None:
        self.config = {"market_data_only": False}
        self.closed = False
        self.orders = {
            "journal-1": {
                "exchange_name": "BINANCE___SWAP",
                "client_order_id": "client-unknown-1",
                "account_id": "account-unknown-1",
            }
        }

    def _unknown_ids(self):
        return {state["client_order_id"] for state in self.orders.values()}


class _FakeExecutionApi:
    """Fake SDK API object; records that no submit/cancel is ever issued."""

    def __init__(self, journal: _FakeExecutionJournal | None = None) -> None:
        self._execution_session = journal or _FakeExecutionJournal()
        self.submit_calls = 0
        self.cancel_calls = 0


class _JournalLinkedOrder:
    def __init__(self) -> None:
        self.info = {
            "client_order_id": "client-unknown-1",
            "external_order_id": "BINANCE___SWAP:9001",
        }
        self.data = types.SimpleNamespace(_name="BTCUSDT")


def _session(
    *,
    store: _FaultStore,
    broker: _FaultBroker | None = None,
    cerebro: _FaultCerebro | None = None,
    provider_query=None,
    cerebro_factory_calls: dict | None = None,
) -> DirectBacktraderSession:
    def store_factory(*, config, profile):
        return store

    def broker_factory(**kwargs):
        return broker

    def cerebro_factory():
        if cerebro_factory_calls is not None:
            cerebro_factory_calls["count"] += 1
        return cerebro

    return DirectBacktraderSession(
        plan=_plan(),
        profile=_profile(),
        run_id=_RUN_ID,
        credentials=_credentials(),
        strategy_id="iteration52-faults",
        grant=_grant(),
        store_factory=store_factory,
        broker_factory=broker_factory if broker is not None else None,
        cerebro_factory=cerebro_factory if cerebro is not None else None,
        provider_query=provider_query,
    )


def test_store_start_failure_reports_bounded_failure_without_fake_cleanup():
    events = _Events()
    store = _FaultStore(events, _profile(), start_error=RuntimeError("provider start failed"))
    broker = _FaultBroker(events)
    cerebro = _FaultCerebro(events)
    session = _session(store=store, broker=broker, cerebro=cerebro)

    report = session.run(lambda: None, seconds=5.0)

    assert report.status == "FAILED"
    assert report.code == "session_failure_runtimeerror"
    assert report.failure_detail == "provider start failed"
    assert report.started is False
    assert report.store_started is False
    assert report.broker_started is False
    assert report.cleanup_steps == ()
    assert report.shutdown["store_stop"] == "not_started"
    assert events == ["store.start"]
    assert store.start_attempts == 1
    assert store.stop_attempts == 0
    assert broker.start_attempts == 0
    assert cerebro.ran == 0


def test_broker_start_failure_stops_started_store_exactly_once():
    events = _Events()
    store = _FaultStore(events, _profile())
    broker = _FaultBroker(events, start_error=RuntimeError("broker start failed"))
    cerebro = _FaultCerebro(events)
    session = _session(store=store, broker=broker, cerebro=cerebro)

    report = session.run(lambda: None, seconds=5.0)

    assert report.status == "FAILED"
    assert report.code == "session_failure_runtimeerror"
    assert report.broker_started is False
    assert report.store_started is True
    assert report.started is True
    assert store.stop_attempts == 1
    assert store.stopped == 1
    assert report.cleanup_steps == ("broker.stop", "store.stop")
    assert report.shutdown["broker_stop"] == "returned"
    assert report.shutdown["store_stop"] == "returned"
    assert report.shutdown["store_connected"] is False
    assert cerebro.ran == 0
    assert events == ["store.start", "broker.start", "broker.stop", "store.stop"]


def test_cerebro_failure_stops_broker_before_store():
    events = _Events()
    store = _FaultStore(events, _profile())
    broker = _FaultBroker(events)
    cerebro = _FaultCerebro(events, run_error=RuntimeError("engine crashed"))
    session = _session(store=store, broker=broker, cerebro=cerebro)

    report = session.run(lambda: None, seconds=5.0)

    assert report.status == "FAILED"
    assert report.code == "session_failure_runtimeerror"
    assert report.failure_detail == "engine crashed"
    assert report.broker_started is True
    assert events == [
        "store.start",
        "broker.start",
        "cerebro.run",
        "broker.stop",
        "store.stop",
    ]
    assert report.cleanup_steps == ("broker.stop", "store.stop")
    assert report.shutdown["broker_stop"] == "returned"
    assert report.shutdown["store_stop"] == "returned"
    assert report.shutdown["store_connected"] is False
    assert report.shutdown["deadline_expired"] is False
    assert cerebro.stop_calls == 0


def test_cerebro_failure_with_broker_stop_error_never_claims_clean_close():
    events = _Events()
    store = _FaultStore(events, _profile())
    broker = _FaultBroker(events, stop_error=RuntimeError("broker stop failed"))
    cerebro = _FaultCerebro(events, run_error=RuntimeError("engine crashed"))
    session = _session(store=store, broker=broker, cerebro=cerebro)

    report = session.run(lambda: None, seconds=5.0)

    assert report.status == "FAILED"
    assert report.code == "session_failure_runtimeerror"
    assert broker.stop_attempts == 1
    assert broker.stopped == 0
    assert store.stop_attempts == 1
    assert store.stopped == 1
    assert report.cleanup_steps == ("broker.stop.failed", "store.stop")
    assert report.shutdown["broker_stop"] == "failed:RuntimeError"
    assert report.shutdown["broker_shutdown_state"]["flattened"] is False
    assert events[-2:] == ["broker.stop", "store.stop"]


def test_store_stop_failure_after_successful_run_reports_partial_shutdown():
    events = _Events()
    store = _FaultStore(events, _profile(), stop_error=RuntimeError("store stop failed"))
    broker = _FaultBroker(events)
    cerebro = _FaultCerebro(events)
    session = _session(store=store, broker=broker, cerebro=cerebro)

    report = session.run(lambda: None, seconds=5.0)

    assert report.status == "PARTIAL"
    assert report.code == "shutdown_incomplete"
    assert store.stop_attempts == 1
    assert store.stopped == 0
    assert report.cleanup_steps == ("broker.stop", "store.stop.failed:RuntimeError")
    assert report.shutdown["broker_stop"] == "returned"
    assert report.shutdown["store_stop"] == "failed:RuntimeError"
    # The failed stop never reached the fake's disconnect step: the report must
    # expose that the store connection was not confirmed closed.
    assert report.shutdown["store_connected"] is True


def test_broker_stop_failure_still_stops_store_and_marks_partial():
    events = _Events()
    store = _FaultStore(events, _profile())
    broker = _FaultBroker(events, stop_error=RuntimeError("broker stop failed"))
    cerebro = _FaultCerebro(events)
    session = _session(store=store, broker=broker, cerebro=cerebro)

    report = session.run(lambda: None, seconds=5.0)

    assert report.status == "PARTIAL"
    assert report.code == "shutdown_incomplete"
    assert broker.stop_attempts == 1
    assert broker.stopped == 0
    assert store.stop_attempts == 1
    assert store.stopped == 1
    assert report.cleanup_steps == ("broker.stop.failed", "store.stop")
    assert report.shutdown["broker_stop"] == "failed:RuntimeError"
    assert report.shutdown["broker_shutdown_state"]["flattened"] is False
    assert report.shutdown["store_stop"] == "returned"
    assert events[-2:] == ["broker.stop", "store.stop"]


def test_pre_run_reconciliation_failure_fails_closed_before_engine():
    events = _Events()
    store = _FaultStore(
        events,
        _profile(),
        reconcile_error=RuntimeError("provider reconcile query failed"),
    )
    broker = _FaultBroker(events)
    cerebro = _FaultCerebro(events)
    session = _session(store=store, broker=broker, cerebro=cerebro)

    report = session.run(lambda: None, seconds=5.0)

    assert report.status == "FAILED"
    assert report.code == "reconcile_snapshot_unavailable"
    assert report.readiness == {}
    assert report.started is True
    assert report.broker_started is False
    assert store.stop_attempts == 1
    assert store.stopped == 1
    assert store.submit_calls == 0
    assert store.cancel_calls == 0
    assert broker.start_attempts == 0
    assert cerebro.ran == 0
    assert events == ["store.start", "store.stop"]
    assert report.shutdown["store_stop"] == "already_stopped"
    assert report.shutdown["store_connected"] is False


def test_unknown_recovery_query_failure_stays_unresolved_without_resend():
    events = _Events()
    store = _FaultStore(
        events,
        _profile(),
        execution_summary={
            "submit_calls": 0,
            "cancel_calls": 0,
            "active_orders": 0,
            "unknown_ids": ["client-unknown-1"],
        },
    )
    api = _FakeExecutionApi()
    store._api = api
    broker = _FaultBroker(events, orders={"order-1": _JournalLinkedOrder()})
    cerebro = _FaultCerebro(events)
    queries: list[tuple[str, str, str]] = []

    def provider_query(exchange_name, request):
        queries.append((exchange_name, request.client_order_id, request.order_id))
        raise RuntimeError("provider query unavailable")

    session = _session(
        store=store,
        broker=broker,
        cerebro=cerebro,
        provider_query=provider_query,
    )

    report = session.run(lambda: None, seconds=5.0)

    assert report.status == "COMPLETED"
    assert report.code == "run_completed"
    recovery = report.shutdown["unknown_recovery"]
    assert recovery["unknown_before"] == 1
    assert recovery["recovered"] == 0
    assert recovery["failures"] == ["RuntimeError"]
    assert report.execution_summary["store"]["unknown_ids"] == ["client-unknown-1"]
    assert queries == [("BINANCE___SWAP", "client-unknown-1", "9001")]
    # Recovery is query-only: the unresolved order is never re-sent.
    assert api.submit_calls == 0
    assert api.cancel_calls == 0
    assert store.submit_calls == 0
    assert store.cancel_calls == 0


def test_shutdown_recovery_crash_still_closes_broker_and_store():
    events = _Events()
    store = _FaultStore(
        events,
        _profile(),
        execution_summary={
            "submit_calls": 0,
            "cancel_calls": 0,
            "active_orders": 0,
            "unknown_ids": ["client-unknown-1"],
        },
    )
    journal = _FakeExecutionJournal()
    journal.orders = None  # unreadable journal: recovery must not take down shutdown
    api = _FakeExecutionApi(journal)
    store._api = api
    broker = _FaultBroker(events, orders={"order-1": _JournalLinkedOrder()})
    cerebro = _FaultCerebro(events)
    queries: list = []

    def provider_query(exchange_name, request):
        queries.append((exchange_name, request))
        raise RuntimeError("must not be reached")

    session = _session(
        store=store,
        broker=broker,
        cerebro=cerebro,
        provider_query=provider_query,
    )

    report = session.run(lambda: None, seconds=5.0)

    assert report.status == "COMPLETED"
    assert report.shutdown["unknown_recovery"] == {
        "status": "unavailable",
        "reason": "recovery_failed",
    }
    assert report.cleanup_steps == ("broker.stop", "store.stop")
    assert broker.stop_attempts == 1
    assert store.stop_attempts == 1
    assert queries == []
    assert api.submit_calls == 0


def test_cerebro_failure_creates_one_scheduler_and_session_is_not_reusable():
    events = _Events()
    store = _FaultStore(events, _profile())
    broker = _FaultBroker(events)
    cerebro = _FaultCerebro(events, run_error=RuntimeError("engine crashed"))
    calls = {"count": 0}
    session = _session(
        store=store,
        broker=broker,
        cerebro=cerebro,
        cerebro_factory_calls=calls,
    )

    report = session.run(lambda: None, seconds=5.0)
    assert report.status == "FAILED"
    assert calls["count"] == 1
    assert broker.start_attempts == 1
    assert broker.stop_attempts == 1
    assert store.stop_attempts == 1

    with pytest.raises(DirectSessionError, match="session_closed"):
        session.run(lambda: None, seconds=1.0)

    # A rejected reuse must not build a second engine or repeat cleanup.
    assert calls["count"] == 1
    assert broker.start_attempts == 1
    assert broker.stop_attempts == 1
    assert store.stop_attempts == 1
    session.close()
    assert broker.stop_attempts == 1
    assert store.stop_attempts == 1


def test_run_after_explicit_close_is_rejected_before_any_factory():
    events = _Events()
    store = _FaultStore(events, _profile())
    broker = _FaultBroker(events)
    cerebro = _FaultCerebro(events)
    session = _session(store=store, broker=broker, cerebro=cerebro)

    session.close()

    with pytest.raises(DirectSessionError, match="session_closed"):
        session.run(lambda: None, seconds=1.0)

    assert store.start_attempts == 0
    assert broker.start_attempts == 0
    assert cerebro.ran == 0


def test_deadline_expiry_stops_a_blocking_engine_within_bound():
    events = _Events()
    store = _FaultStore(events, _profile())
    broker = _FaultBroker(events)
    cerebro = _BlockingCerebro(events)
    session = _session(store=store, broker=broker, cerebro=cerebro)

    started = time.monotonic()
    report = session.run(lambda: None, seconds=0.05)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    assert cerebro.ran == 1
    assert cerebro.stop_calls >= 1
    assert isinstance(report.status, str) and report.status
    assert isinstance(report.code, str) and report.code
    assert report.status == "COMPLETED"
    assert report.code == "run_completed"
    assert report.shutdown["deadline_expired"] is True
    assert report.shutdown["broker_stop"] == "returned"
    assert report.shutdown["store_stop"] == "returned"
    assert report.cleanup_steps == ("broker.stop", "store.stop")
