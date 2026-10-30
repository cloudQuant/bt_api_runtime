"""Source-fake vertical slice through the real SDK read facades and Backtrader."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import threading
import time

import backtrader as bt
import pytest
from backtrader.brokers.btapibroker import BtApiBroker
from backtrader.cerebro import Cerebro
from backtrader.feeds.btapifeed import BtApiFeed
from backtrader.stores.btapistore import BtApiStore
from backtrader.stores.service_read_proxy import ServiceFeedBinding, ServiceReadProxy
from bt_api_base.account_protocol import (
    AccountBalance,
    AccountKey,
    AccountObservation,
    AccountResourceKey,
    AccountValuation,
)
from bt_api_base.market_history_protocol import (
    MarketBar,
    MarketHistoryPage,
    MarketHistoryRequest,
)
from bt_api_base.market_protocol import MarketInstrumentKey, MarketSubscriptionKey
from bt_api_base.service_protocol import (
    WireEnvelope,
    WireIdentity,
    WireMessageKind,
    WireRole,
    WireScope,
)
from bt_api_py.service_account import ServiceAccountClient
from bt_api_py.service_config import parse_client_config
from bt_api_py.service_market import ServiceMarketClient
from bt_api_py.service_registry import (
    PrincipalGrant,
    ResourceGrant,
    ServiceCapability,
    ServiceRegistry,
)
from bt_api_py.service_runtime import ServiceClientRuntime
from bt_api_run_contract import (
    Environment,
    ReferencesV2,
    TrustedRegistryV2,
    TrustedRouteV2,
)
from bt_api_runtime import (
    FeedSpec,
    RouteBindingV2,
    RuntimeAssemblyError,
    RuntimeBindingsV2,
    assemble_run,
)

_STRATEGY_REF = "strategy/shared_configured_observer"
# The I52 route contract intentionally uses its public slash-qualified ref,
# while the current SDK ClientConfig ref grammar rejects '/'. Keep the
# translation explicit at the SDK boundary instead of weakening either schema.
_SDK_STRATEGY_REF_BY_ROUTE = {
    _STRATEGY_REF: "strategy.shared_configured_observer",
}
_PRODUCT_REF = "product/synthetic_spot"
_SDK_PRODUCT_TYPE_BY_ROUTE = {_PRODUCT_REF: "spot"}
_DOMAIN_REF = "synthetic:source-fake"
_SERVICE_ACCOUNT_OPERATIONS = frozenset({"account.snapshot"})
_SERVICE_MARKET_OPERATIONS = frozenset(
    {
        "market.subscribe",
        "market.unsubscribe",
        "market.snapshot",
        "market.head",
        "market.read_after",
        "market.history",
    }
)
_STREAM_EPOCH = "synthetic-epoch-one"
_BAR_OPEN = "2026-10-01T12:00:00Z"
_BAR_CLOSE = "2026-10-01T12:01:00Z"
_OBSERVED_AT = "2026-10-01T12:02:00Z"
_DATA_NAME = "SYNTH-USD"
_SAFE_SETUP_EXCEPTION_TYPES = frozenset(
    {
        "AssertionError",
        "AttributeError",
        "KeyError",
        "OSError",
        "RuntimeError",
        "ServiceAccountError",
        "ServiceMarketError",
        "ServiceRuntimeError",
        "ServiceRegistryError",
        "TypeError",
        "ValueError",
    }
)
_SAFE_SETUP_ERROR_CODES = frozenset(
    {
        "ACCOUNT_BINDING_UNAVAILABLE",
        "ACCOUNT_COVERAGE_MISMATCH",
        "ACCOUNT_IDENTITY_MISMATCH",
        "ACCOUNT_READ_FAILED",
        "CAPABILITY_UNAVAILABLE",
        "CLOSE_FAILED",
        "INVALID_ACCOUNT_MOUNT",
        "INVALID_CONFIG",
        "INVALID_MARKET_MOUNT",
        "INVALID_PEER",
        "INVALID_SCOPE",
        "INVALID_SERVICE_REPLY",
        "MARKET_BINDING_UNAVAILABLE",
        "MARKET_ENDPOINT_REQUIRED",
        "MARKET_WIRE_VERSION_MISMATCH",
        "PROTOCOL_VERSION_MISMATCH",
        "REQUIRED_REGISTRATION_FAILED",
        "ROLE_MISMATCH",
        "SERVICE_NOT_READY",
    }
)


class _SameStrategy(bt.Strategy):
    params = (("enabled", False),)

    def __init__(self):
        self.observations = []
        self.order = None

    def next(self):
        self.observations.append((self.p.enabled, float(self.data.close[0])))
        self.order = self.buy(size=1)
        self.env.runstop()


class _SourceFakeEventSubscriber:
    """Synchronous provider-I/O leaf; it creates no worker or network socket."""

    def __init__(self, *, close_failures: int = 0) -> None:
        self.receive_calls = 0
        self.close_calls = 0
        self.close_failures_remaining = close_failures
        self.closed = False

    def receive(self, *, timeout: float | None = None):
        if self.closed:
            raise AssertionError("receive called after subscriber close")
        if timeout is not None and timeout < 0:
            raise AssertionError("subscriber received a negative timeout")
        self.receive_calls += 1
        return None

    def close(self) -> None:
        self.close_calls += 1
        if self.close_failures_remaining:
            self.close_failures_remaining -= 1
            raise OSError("synthetic subscriber close failure")
        self.closed = True


class _SyntheticReadService:
    """Synthetic service handler used behind the fake request/reply leaf."""

    def __init__(self, account_key, resource, instrument, account_ref) -> None:
        self.account_key = account_key
        self.resource = resource
        self.instrument = instrument
        self.account_ref = account_ref
        self.market_registrations: set[str] = set()
        self.operations: list[str] = []

    def account_snapshot(self, context, arguments):
        self.operations.append("account.snapshot")
        if context.scope.account_ref != self.account_ref or arguments != {"limit": 1}:
            raise AssertionError("account read escaped its synthetic scope or bound request")
        observation = AccountObservation(
            account_key=self.account_key,
            observation_id="source-fake-account-observation",
            coverage=(self.resource,),
            balances=(
                AccountBalance(
                    resource=self.resource,
                    currency="USD",
                    total="100",
                    available="100",
                    locked="0",
                    observed_at_utc=_OBSERVED_AT,
                ),
            ),
            positions=(),
            valuation=AccountValuation(
                currency="USD",
                available_cash="100",
                equity="100",
                observed_at_utc=_OBSERVED_AT,
            ),
            started_at_utc="2026-10-01T12:01:00Z",
            completed_at_utc=_OBSERVED_AT,
            consistency="provider_atomic",
            balances_complete=True,
            positions_complete=True,
            open_orders=(),
            open_orders_complete=True,
        )
        cursor = 1
        generation = "source-fake-account-generation"
        return {
            "requested_cursor": None,
            "snapshot": {
                "account_scope": "synthetic-account-scope",
                "strategy_scope": "synthetic-strategy-scope",
                "snapshot_id": "source-fake-snapshot-one",
                "cursor": cursor,
                "payload": {
                    "schema_id": "sdk.account.state",
                    "schema_version": 2,
                    "session_generation": generation,
                    "observation": observation.to_dict(),
                },
                "captured_at": time.time(),
            },
            "events": [],
            "next_cursor": cursor,
            "latest_cursor": cursor,
            "recovered_from_snapshot": False,
            "snapshot_source": "persisted_journal_baseline",
            "state_status": {
                "session_generation": generation,
                "ready": True,
                "fresh_for_seconds": 60.0,
            },
        }

    def market(self, context, arguments, operation: str):
        self.operations.append(operation)
        key = MarketSubscriptionKey.from_dict(arguments["key"])
        if key.instrument != self.instrument:
            raise AssertionError("market read escaped its synthetic instrument binding")
        stream_id = key.stream_id
        registration_id = context.registration_id

        if operation == "market.subscribe":
            already_subscribed = registration_id in self.market_registrations
            self.market_registrations.add(registration_id)
            return {
                "stream_id": stream_id,
                "epoch": _STREAM_EPOCH,
                "head": 0,
                "oldest_sequence": 1,
                "already_subscribed": already_subscribed,
            }
        if operation == "market.unsubscribe":
            self.market_registrations.discard(registration_id)
            return {
                "stream_id": stream_id,
                "epoch": _STREAM_EPOCH,
                "head": 0,
                "upstream_released": not self.market_registrations,
            }
        if operation == "market.history":
            request = MarketHistoryRequest(
                key=key,
                start_time_utc=arguments["start_time_utc"],
                end_time_utc=arguments["end_time_utc"],
                limit=arguments["limit"],
            )
            bar = MarketBar(
                open_time_utc=_BAR_OPEN,
                close_time_utc=_BAR_CLOSE,
                open="100",
                high="101",
                low="99",
                close="100.5",
                volume="2",
            )
            return MarketHistoryPage(
                request=request,
                bars=(bar,),
                complete=True,
                next_start_time_utc=None,
                observed_at_utc=_OBSERVED_AT,
            ).to_dict()
        if registration_id not in self.market_registrations:
            raise AssertionError("market read arrived without a synthetic subscription")
        if operation == "market.snapshot":
            return {
                "snapshot_kind": "latest_event",
                "complete": False,
                "stream_id": stream_id,
                "epoch": _STREAM_EPOCH,
                "head": 0,
                "event": None,
            }
        if operation == "market.head":
            return {
                "stream_id": stream_id,
                "epoch": _STREAM_EPOCH,
                "head": 0,
                "oldest_sequence": 1,
                "latest_observed_at_utc": None,
            }
        if operation == "market.read_after":
            return {
                "stream_id": stream_id,
                "epoch": _STREAM_EPOCH,
                "head": 0,
                "oldest_sequence": 1,
                "after": arguments["after"],
                "next_after": arguments["after"],
                "limit": arguments["limit"],
                "has_more": False,
                "gap": False,
                "gap_reason": None,
                "events": [],
            }
        raise AssertionError(f"unexpected synthetic operation: {operation}")


class _LocalRequestReplyTransport:
    """Only the outer RPC/I/O leaf is replaced; the SDK runtime stays real."""

    def __init__(self, registry: ServiceRegistry, principal_ref: str, peer: WireIdentity):
        self.registry = registry
        self.principal_ref = principal_ref
        self.peer = peer
        self.operations: list[str] = []
        self.closed = False

    def request(self, request: WireEnvelope, *, timeout: float) -> WireEnvelope:
        if self.closed:
            raise AssertionError("RPC requested after transport close")
        if not 0 < timeout <= 1.0:
            raise AssertionError("RPC timeout was not explicitly bounded")
        self.operations.append(request.header.operation)
        result = self.registry.handle(self.principal_ref, request)
        return WireEnvelope(
            replace(
                request.header,
                kind=WireMessageKind.REPLY,
                message_id="reply-" + request.header.message_id,
                correlation_id=request.header.message_id,
                identity=self.peer,
            ),
            result,
        )

    def close(self) -> None:
        self.closed = True


def _route(environment: Environment) -> TrustedRouteV2:
    suffix = environment.value
    return TrustedRouteV2(
        environment,
        ReferencesV2(
            strategy=_STRATEGY_REF,
            provider="provider/service_boundary",
            endpoint=f"endpoint/{suffix}",
            account=f"account/{suffix}",
            credential=f"credential/{suffix}",
            scope=f"scope/{suffix}",
            product=_PRODUCT_REF,
            data_source="data_source/market_feed",
            clock="clock/exchange_utc",
        ),
        ("account.read", "market.read"),
        (("enabled", "bool", True),),
    )


def _sdk_strategy_ref(route_strategy_ref: str) -> str:
    try:
        return _SDK_STRATEGY_REF_BY_ROUTE[route_strategy_ref]
    except KeyError:
        raise AssertionError("synthetic route has no SDK strategy-ref mapping") from None


def _sdk_product_type(route_product_ref: str) -> str:
    try:
        return _SDK_PRODUCT_TYPE_BY_ROUTE[route_product_ref]
    except KeyError:
        raise AssertionError("synthetic route has no SDK product-type mapping") from None


class _RetainedResourceOwner:
    """Strongly retain uncertain read-facade/runtime/registry closure state."""

    def __init__(self) -> None:
        self._retained: list[dict[str, object]] = []

    @property
    def retained(self) -> tuple[dict[str, object], ...]:
        return tuple(self._retained)

    def close_resources(
        self,
        resources: dict[str, object],
        *,
        facade_close_confirmed: bool = False,
    ) -> bool:
        try:
            if not facade_close_confirmed:
                proxy = resources.get("proxy")
                if proxy is not None:
                    proxy.disconnect()
                else:
                    errors = []
                    for name in ("market_client", "account_client"):
                        facade = resources.get(name)
                        if facade is not None:
                            try:
                                facade.close()
                            except BaseException as error:
                                errors.append(error)
                    if errors:
                        raise errors[0]

            runtime = resources.get("runtime")
            if runtime is not None:
                runtime.close()
                if runtime.state != "closed":
                    raise RuntimeError("runtime_close_unconfirmed")

            registry = resources.get("registry")
            if registry is not None:
                registry.close()
        except BaseException:
            self.retain(resources)
            return False

        self._forget(resources)
        return True

    def retain(self, resources: dict[str, object]) -> None:
        if not any(item is resources for item in self._retained):
            self._retained.append(resources)

    def retry_one(self) -> bool:
        if not self._retained:
            return False
        resources = self._retained[0]
        return self.close_resources(resources)

    def _forget(self, resources: dict[str, object]) -> None:
        self._retained[:] = [item for item in self._retained if item is not resources]


_RETAINED_RESOURCE_OWNER = _RetainedResourceOwner()


def _run_config(route: TrustedRouteV2) -> str:
    refs = route.references
    lines = [
        "schema_version: 2",
        f"mode: {route.environment.value}",
        f"environment: {route.environment.value}",
        "references:",
    ]
    lines.extend(
        f"  {name}: {getattr(refs, name)}"
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
    )
    lines.extend(
        (
            "plugins: {gateway: false, risk: false, monitor: false, execution: false, runtime: false}",
            "strategy_parameters:",
            "  enabled: true",
            "requested_capabilities:",
            "  - account.read",
            "  - market.read",
        )
    )
    return "\n".join(lines) + "\n"


def _make_stack(
    environment: Environment,
    tmp_path,
    *,
    subscriber_close_failures: int = 0,
):
    suffix = environment.value
    phase = "synthetic_domain"
    registry = None
    runtime = None
    proxy = None
    account_client = None
    market_client = None
    subscriber = None
    transport = None
    service = None
    try:
        service_id = f"synthetic-service-{suffix}"
        instance_id = f"synthetic-gateway-{suffix}"
        account_ref = f"synthetic-account-{suffix}"
        account_scope_ref = f"synthetic-account-scope-{suffix}"
        market_scope_ref = f"synthetic-market-scope-{suffix}"
        transport_ref = f"synthetic-client-{suffix}"
        client = WireIdentity(
            WireRole.STRATEGY_CLIENT,
            f"synthetic-principal-{suffix}",
            _sdk_strategy_ref(_STRATEGY_REF),
        )
        peer = WireIdentity(
            WireRole.SERVICE,
            f"synthetic-service-principal-{suffix}",
            service_id,
        )
        account_key = AccountKey(
            provider="synthetic",
            venue="source-fake",
            environment=suffix,
            account_id=account_ref,
        )
        resource = AccountResourceKey("spot", "synthetic-cash")
        instrument = MarketInstrumentKey(
            provider="synthetic",
            venue="source-fake",
            environment=suffix,
            product_type=_sdk_product_type(_PRODUCT_REF),
            instrument_id="synthetic-instrument",
            native_symbol="SYNTH-USD",
        )
        market_key = MarketSubscriptionKey(
            source_ref="synthetic-market-source",
            entitlement_ref="synthetic-market-entitlement",
            instrument=instrument,
            channel="bar",
            timeframe="1m",
        )
        account_scope = WireScope(
            _DOMAIN_REF,
            account_ref=account_ref,
            strategy_ref=_sdk_strategy_ref(_STRATEGY_REF),
        )
        market_scope = WireScope(
            _DOMAIN_REF,
            strategy_ref=_sdk_strategy_ref(_STRATEGY_REF),
        )

        capability = (
            ServiceCapability(
                instance_id, "service.account_read", _SERVICE_ACCOUNT_OPERATIONS
            ),
            ServiceCapability(
                instance_id, "service.market_source", _SERVICE_MARKET_OPERATIONS
            ),
        )
        grants = (
            ResourceGrant(
                instance_id,
                "service.account_read",
                account_scope_ref,
                account_scope,
                _SERVICE_ACCOUNT_OPERATIONS,
            ),
            ResourceGrant(
                instance_id,
                "service.market_source",
                market_scope_ref,
                market_scope,
                _SERVICE_MARKET_OPERATIONS,
            ),
        )
        phase = "service_registry_create"
        registry = ServiceRegistry(
            tmp_path / f"source-fake-{suffix}.sqlite3",
            service_id=service_id,
            capabilities=capability,
            grants={client.principal_ref: (PrincipalGrant(client, grants),)},
            policy_version="source-fake-policy-v1",
            requirements_version="source-fake-requirements-v1",
            lease_seconds=30,
        )
        service = _SyntheticReadService(account_key, resource, instrument, account_ref)
        phase = "service_registry_handlers"
        registry.register_handler(
            instance_id, "account.snapshot", service.account_snapshot
        )
        for operation in _SERVICE_MARKET_OPERATIONS:
            registry.register_handler(
                instance_id,
                operation,
                lambda context, arguments, selected=operation: service.market(
                    context, arguments, selected
                ),
            )
        registry.set_ready(True)

        transport = _LocalRequestReplyTransport(registry, client.principal_ref, peer)
        raw_config = {
            "client_config_schema_version": 1,
            "process_role": "strategy_client",
            "registration": {
                "strategy_ref": _sdk_strategy_ref(_STRATEGY_REF),
            },
            "clients": {
                "gateway": {
                    "config_schema_version": 1,
                    "settings": {
                        "bindings": [
                            {
                                "mount": "store.account",
                                "service_id": service_id,
                                "instance_id": instance_id,
                                "scope_ref": account_scope_ref,
                                "account_ref": account_ref,
                                "transport_ref": transport_ref,
                            },
                            {
                                "mount": "store.market",
                                "service_id": service_id,
                                "instance_id": instance_id,
                                "scope_ref": market_scope_ref,
                                "transport_ref": transport_ref,
                            },
                        ],
                        "options": {"request_timeout_ms": 1_000},
                    },
                }
            },
            "communication": {
                "config_schema_version": 1,
                "profiles": {
                    transport_ref: {
                        "backend": "zmq",
                        "config_schema_version": 1,
                        "endpoints": {
                            "rpc_connect": "tcp://127.0.0.1:1",
                            "market_connect": "tcp://127.0.0.1:2",
                        },
                        "security_profile_ref": "source-fake-keys",
                        "options": {
                            "request_timeout_ms": 1_000,
                            "high_water_mark": 16,
                            "max_message_bytes": 262_144,
                        },
                    }
                },
            },
        }
        phase = "client_config_parse"
        parsed_config = parse_client_config(raw_config)
        phase = "runtime_construct"
        runtime = ServiceClientRuntime(
            parsed_config,
            identity=client,
            scope_resolver=lambda scope_ref: {
                account_scope_ref: account_scope,
                market_scope_ref: market_scope,
            }[scope_ref],
            peer_resolver=lambda requested_service_id: (
                peer if requested_service_id == service_id else None
            ),
            transport_factory=lambda _profile, _peer: transport,
            run_id=f"source-fake-run-{suffix}",
            requested_operations={
                "store.account": _SERVICE_ACCOUNT_OPERATIONS,
                "store.market": _SERVICE_MARKET_OPERATIONS,
            },
        )
        phase = "runtime_start"
        runtime.start()
        phase = "account_facade_construct"
        account_client = ServiceAccountClient(
            runtime,
            "store.account",
            account_ref=account_ref,
            expected_account=account_key,
            required_coverage=(resource,),
            require_open_orders=True,
        )
        subscriber = _SourceFakeEventSubscriber(
            close_failures=subscriber_close_failures
        )
        phase = "market_facade_construct"
        market_client = ServiceMarketClient(
            runtime,
            "store.market",
            market_key,
            subscriber_factory=lambda _binding, _scope: subscriber,
            stream_scope=WireScope(_DOMAIN_REF),
            head_check_interval_seconds=60.0,
        )
        phase = "read_proxy_construct"
        proxy = ServiceReadProxy(
            account_client,
            {
                _DATA_NAME: ServiceFeedBinding(
                    market_client=market_client,
                    resource=resource,
                    qty_unit="base_units",
                    timeframe="1m",
                    history_start_utc=_BAR_OPEN,
                    history_end_utc=_BAR_CLOSE,
                )
            },
            valuation_currency="USD",
            position_mode="net",
            clock=lambda: datetime(2026, 10, 1, 12, 2, tzinfo=timezone.utc),
        )
    except BaseException as error:
        exception_type = type(error).__name__
        if exception_type not in _SAFE_SETUP_EXCEPTION_TYPES:
            exception_type = "other_exception"
        raw_code = getattr(error, "code", None)
        safe_code = raw_code if type(raw_code) is str and raw_code in _SAFE_SETUP_ERROR_CODES else "unlisted_code"
        resources = {
            "runtime": runtime,
            "account_client": account_client,
            "market_client": market_client,
            "proxy": proxy,
            "subscriber": subscriber,
            "transport": transport,
            "service": service,
            "registry": registry,
        }
        cleanup_state = (
            "resources_closed"
            if _RETAINED_RESOURCE_OWNER.close_resources(resources)
            else "resources_retained"
        )
        _SETUP_DIAGNOSTICS[environment] = (
            phase,
            exception_type,
            safe_code,
            cleanup_state,
        )
        raise

    return {
        "runtime": runtime,
        "account_client": account_client,
        "market_client": market_client,
        "proxy": proxy,
        "subscriber": subscriber,
        "transport": transport,
        "service": service,
        "registry": registry,
    }


_SETUP_DIAGNOSTICS: dict[Environment, tuple[str, str, str, str]] = {}


def _runtime_bindings(tmp_path, stacks):
    routes = {
        environment: _route(environment)
        for environment in (Environment.PAPER, Environment.LIVE)
    }

    def provider_factory(environment):
        def create():
            stack = _make_stack(environment, tmp_path)
            stacks[environment] = stack
            return stack["proxy"]

        return create

    contract_registry = TrustedRegistryV2(
        (routes[Environment.PAPER], routes[Environment.LIVE])
    )
    bindings = RuntimeBindingsV2(
        contract_registry,
        tuple(
            RouteBindingV2(
                routes[environment],
                _SameStrategy,
                (FeedSpec(_DATA_NAME),),
                provider_factory(environment),
            )
            for environment in (Environment.PAPER, Environment.LIVE)
        ),
    )
    return bindings, routes


def test_real_sdk_facades_run_same_strategy_in_paper_and_live_without_network(tmp_path):
    _SETUP_DIAGNOSTICS.clear()
    assert _RETAINED_RESOURCE_OWNER.retained == ()
    stacks = {}
    bindings, routes = _runtime_bindings(tmp_path, stacks)
    outcomes = []

    for environment in (Environment.PAPER, Environment.LIVE):
        session = None
        normal_proxy_close_confirmed = False
        stop_requested = threading.Event()
        timer = None
        try:
            try:
                session = assemble_run(_run_config(routes[environment]), bindings)
            except RuntimeAssemblyError as error:
                diagnostic = _SETUP_DIAGNOSTICS.get(environment)
                if error.code == "provider_factory_failed" and diagnostic is not None:
                    phase, exception_type, safe_code, cleanup_state = diagnostic
                    raise AssertionError(
                        "synthetic_provider_setup_failed:"
                        f"{phase}:{exception_type}:{safe_code}:{cleanup_state}"
                    ) from None
                raise

            def request_stop_at_deadline():
                stop_requested.set()
                session.request_stop()

            timer = threading.Timer(10.0, request_stop_at_deadline)
            timer.daemon = True
            timer.start()
            (strategy,) = session.run()
            # RunSession.run returns only after its real Store/proxy shutdown checks.
            assert stop_requested.is_set() is False
            stack = stacks[environment]
            assert session._store._service_read_only_stopped is True
            assert stack["proxy"].is_connected is False
            normal_proxy_close_confirmed = True
            assert type(stack["runtime"]) is ServiceClientRuntime
            assert type(stack["account_client"]) is ServiceAccountClient
            assert type(stack["market_client"]) is ServiceMarketClient
            assert type(stack["proxy"]) is ServiceReadProxy
            assert type(strategy) is _SameStrategy
            assert type(strategy.env) is Cerebro
            assert type(strategy.datas[0]) is BtApiFeed
            assert type(strategy.broker) is BtApiBroker
            assert type(strategy.broker.store) is BtApiStore
            assert strategy.p.enabled is True
            assert routes[environment].references.product == _PRODUCT_REF
            assert stack["service"].instrument.product_type == _sdk_product_type(
                routes[environment].references.product
            )
            assert strategy.observations == [(True, 100.5)]
            assert strategy.order is not None
            assert strategy.order.status == strategy.order.Rejected
            assert strategy.order.info.get("error_code") == "market_data_only"
            assert strategy.broker.get_market_data_only_audit() == {
                "submit_rejected": 1,
                "cancel_rejected": 0,
                "batch_cancel_rejected": 0,
                "total_rejected": 1,
            }
            assert stack["proxy"].is_connected is False
            assert stack["service"].market_registrations == set()
            assert stack["subscriber"].closed is True
            assert stack["transport"].closed is False
            assert stack["runtime"].state == "registered"
            assert "account.snapshot" in stack["service"].operations
            assert "market.subscribe" in stack["service"].operations
            assert "market.history" in stack["service"].operations
            assert "market.unsubscribe" in stack["service"].operations
            for operations in (
                stack["transport"].operations,
                stack["service"].operations,
            ):
                assert not any(
                    operation.startswith(("orders.", "execution."))
                    for operation in operations
                )
            outcomes.append((environment, type(strategy), tuple(strategy.observations)))
        finally:
            stack = stacks.get(environment)
            try:
                if timer is not None:
                    timer.cancel()
                    timer.join(timeout=2.0)
                    assert timer.is_alive() is False
            finally:
                if stack is not None and session is not None and not normal_proxy_close_confirmed:
                    try:
                        session.close()
                    except Exception:
                        # The explicit owner below retries the real facade and
                        # retains all resources if that close remains uncertain.
                        pass
                if stack is not None:
                    # The test owns the runtime, but only a normal session close
                    # confirms the facade boundary without another proxy call.
                    _RETAINED_RESOURCE_OWNER.close_resources(
                        stack,
                        facade_close_confirmed=normal_proxy_close_confirmed,
                    )

    assert outcomes == [
        (Environment.PAPER, _SameStrategy, ((True, 100.5),)),
        (Environment.LIVE, _SameStrategy, ((True, 100.5),)),
    ]
    assert type(outcomes[0][1]) is type(outcomes[1][1]) is type
    assert _RETAINED_RESOURCE_OWNER.retained == ()
    for environment in (Environment.PAPER, Environment.LIVE):
        stack = stacks[environment]
        assert stack["runtime"].state == "closed"
        assert stack["runtime"].cleanup_failures == 0
        assert stack["runtime"].unregister_uncertainties == 0
        assert stack["runtime"].unknown_registration_count == 0
        assert stack["transport"].closed is True
        with pytest.raises(RuntimeError):
            stack["registry"].set_ready(True)


def test_failed_proxy_close_is_retained_until_owner_retry(tmp_path):
    assert _RETAINED_RESOURCE_OWNER.retained == ()
    stack = _make_stack(
        Environment.PAPER,
        tmp_path,
        subscriber_close_failures=1,
    )
    stack["market_client"].start()
    assert stack["service"].market_registrations

    # A false connection flag during ServiceReadProxy's closing state is not a
    # close receipt. The real proxy raised, so runtime and Registry stay owned.
    assert _RETAINED_RESOURCE_OWNER.close_resources(stack) is False
    assert _RETAINED_RESOURCE_OWNER.retained == (stack,)
    assert stack["runtime"].state == "registered"
    assert stack["registry"].ready is True
    assert stack["transport"].closed is False
    assert stack["subscriber"].close_calls == 1
    assert stack["subscriber"].closed is False

    assert _RETAINED_RESOURCE_OWNER.retry_one() is True
    assert _RETAINED_RESOURCE_OWNER.retained == ()
    assert stack["proxy"].is_connected is False
    assert stack["runtime"].state == "closed"
    assert stack["transport"].closed is True
    assert stack["subscriber"].close_calls == 2
    assert stack["subscriber"].closed is True
    with pytest.raises(RuntimeError):
        stack["registry"].set_ready(True)
