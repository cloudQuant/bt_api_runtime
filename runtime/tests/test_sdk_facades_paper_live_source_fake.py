"""Source-fake vertical slice through the real SDK read facades and Backtrader."""

from __future__ import annotations

import builtins
import sys
import threading
import time
import traceback
from dataclasses import fields, replace
from datetime import datetime, timezone
from types import ModuleType

import backtrader as bt
import pytest
from backtrader.brokers.btapibroker import BtApiBroker
from backtrader.cerebro import Cerebro
from backtrader.feeds.btapifeed import BtApiFeed
from backtrader.stores.btapistore import BtApiStore
from backtrader.stores.service_read_proxy import ServiceReadProxy
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
from bt_api_base.market_protocol import (
    MarketInstrumentKey,
    MarketSourceDescriptor,
    MarketSubscriptionKey,
)
from bt_api_base.service_protocol import (
    WireEnvelope,
    WireIdentity,
    WireMessageKind,
    WireRole,
    WireScope,
)
from bt_api_py.service_account import ServiceAccountClient
from bt_api_py.service_config import parse_client_config
from bt_api_py.service_market import (
    ExpectedMarketSourceIdentity,
    ServiceMarketClient,
    ServiceMarketError,
)
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
    SdkClockBinding,
    SdkEnvironmentMapping,
    SdkMarketSourceExpectation,
    SdkReadOnlyError,
    SdkReadOnlyFacadeFactory,
    SdkReadOnlySpecV2,
    SdkRouteReference,
    SdkRouteReferencesV2,
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
_BAR_TWO_CLOSE = "2026-10-01T12:02:00Z"
_OBSERVED_AT = "2026-10-01T12:02:00Z"
_SECOND_OBSERVED_AT = "2026-10-01T12:03:00Z"
_DATA_NAME = "SYNTH-USD"
_SOURCE_EXPECTATION = {
    "source": "source-fake-provider-route",
    "clock_domain_id": "source-fake-clock-domain",
    "clock_source_session_id": "source-fake-clock-session",
    "clock_source": "source-fake-clock-source",
}
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

    def close(self) -> None:
        self.close_calls += 1
        if self.close_failures_remaining:
            self.close_failures_remaining -= 1
            raise OSError("synthetic subscriber close failure")
        self.closed = True


class _SyntheticReadService:
    """Synthetic service handler used behind the fake request/reply leaf."""

    def __init__(
        self,
        account_key,
        resource,
        instrument,
        account_ref,
        *,
        source_identity_overrides=(),
        history_bar_count=1,
    ) -> None:
        self.account_key = account_key
        self.resource = resource
        self.instrument = instrument
        self.account_ref = account_ref
        self.market_registrations: set[str] = set()
        self.operations: list[str] = []
        self.source_identity_overrides = tuple(source_identity_overrides)
        self.subscribe_calls = 0
        self.history_calls = 0
        self.history_bar_count = history_bar_count

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
            reply = {
                "stream_id": stream_id,
                "epoch": _STREAM_EPOCH,
                "head": 0,
                "oldest_sequence": 1,
                "already_subscribed": already_subscribed,
            }
            if arguments.get("require_source_descriptor") is True:
                descriptor_identity = dict(_SOURCE_EXPECTATION)
                if self.subscribe_calls < len(self.source_identity_overrides):
                    override = self.source_identity_overrides[self.subscribe_calls]
                    if override is not None:
                        descriptor_identity.update(override)
                self.subscribe_calls += 1
                reply["source_descriptor"] = MarketSourceDescriptor(
                    key=key,
                    stream_id=stream_id,
                    epoch=_STREAM_EPOCH,
                    provider=key.instrument.provider,
                    connection_generation=1,
                    **descriptor_identity,
                ).to_dict()
            return reply
        if operation == "market.unsubscribe":
            self.market_registrations.discard(registration_id)
            return {
                "stream_id": stream_id,
                "epoch": _STREAM_EPOCH,
                "head": 0,
                "upstream_released": not self.market_registrations,
            }
        if operation == "market.history":
            self.history_calls += 1
            request = MarketHistoryRequest(
                key=key,
                start_time_utc=arguments["start_time_utc"],
                end_time_utc=arguments["end_time_utc"],
                limit=arguments["limit"],
            )
            bars = (
                MarketBar(
                    open_time_utc=_BAR_OPEN,
                    close_time_utc=_BAR_CLOSE,
                    open="100",
                    high="101",
                    low="99",
                    close="100.5",
                    volume="2",
                ),
                MarketBar(
                    open_time_utc=_BAR_CLOSE,
                    close_time_utc=_BAR_TWO_CLOSE,
                    open="100.5",
                    high="102",
                    low="100",
                    close="101",
                    volume="3",
                ),
            )
            return MarketHistoryPage(
                request=request,
                bars=bars[: self.history_bar_count],
                complete=True,
                next_start_time_utc=None,
                observed_at_utc=(
                    _SECOND_OBSERVED_AT if self.history_bar_count > 1 else _OBSERVED_AT
                ),
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

    def __init__(
        self,
        registry: ServiceRegistry,
        principal_ref: str,
        peer: WireIdentity,
        *,
        drop_unregister_response: bool = False,
        drop_register_response: bool = False,
    ):
        self.registry = registry
        self.principal_ref = principal_ref
        self.peer = peer
        self.operations: list[str] = []
        self.closed = False
        self.drop_unregister_response = drop_unregister_response
        self.drop_register_response = drop_register_response

    def request(self, request: WireEnvelope, *, timeout: float) -> WireEnvelope:
        if self.closed:
            raise AssertionError("RPC requested after transport close")
        if not 0 < timeout <= 1.0:
            raise AssertionError("RPC timeout was not explicitly bounded")
        self.operations.append(request.header.operation)
        result = self.registry.handle(self.principal_ref, request)
        if request.header.operation == "service.register" and self.drop_register_response:
            self.drop_register_response = False
            raise OSError("synthetic register response loss")
        if (
            request.header.operation == "service.unregister"
            and self.drop_unregister_response
        ):
            self.drop_unregister_response = False
            raise OSError("synthetic unregister response loss")
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
    drop_unregister_response: bool = False,
    drop_register_response: bool = False,
    source_identity_overrides=(),
    history_bar_count: int = 1,
    history_end_utc: str | None = None,
    route: TrustedRouteV2 | None = None,
):
    suffix = environment.value
    route = route or _route(environment)
    service_id = f"synthetic-service-{suffix}"
    instance_id = f"synthetic-gateway-{suffix}"
    account_ref = f"synthetic-account-{suffix}"
    account_scope_ref = f"synthetic-account-scope-{suffix}"
    market_scope_ref = f"synthetic-market-scope-{suffix}"
    transport_ref = f"synthetic-client-{suffix}"
    sdk_strategy_ref = _sdk_strategy_ref(route.references.strategy)
    client = WireIdentity(
        WireRole.STRATEGY_CLIENT,
        f"synthetic-principal-{suffix}",
        sdk_strategy_ref,
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
        product_type=_sdk_product_type(route.references.product),
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
        strategy_ref=sdk_strategy_ref,
    )
    market_scope = WireScope(
        _DOMAIN_REF,
        strategy_ref=sdk_strategy_ref,
    )
    stream_scope = WireScope(_DOMAIN_REF)
    scope_dimension_names = {
        "account_ref",
        "account_refs",
        "strategy_ref",
        "run_id",
        "manual_book_ref",
        "portfolio_ref",
        "group_id",
        "leg_id",
    }
    scope_fields = tuple(field.name for field in fields(WireScope))
    scope_context_field = next(
        name for name in scope_fields if name not in scope_dimension_names
    )

    capability = (
        ServiceCapability(instance_id, "service.account_read", _SERVICE_ACCOUNT_OPERATIONS),
        ServiceCapability(instance_id, "service.market_source", _SERVICE_MARKET_OPERATIONS),
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
    registry = ServiceRegistry(
        tmp_path / f"source-fake-{suffix}.sqlite3",
        service_id=service_id,
        capabilities=capability,
        grants={client.principal_ref: (PrincipalGrant(client, grants),)},
        policy_version="source-fake-policy-v1",
        requirements_version="source-fake-requirements-v1",
        lease_seconds=30,
    )
    service = _SyntheticReadService(
        account_key,
        resource,
        instrument,
        account_ref,
        source_identity_overrides=source_identity_overrides,
        history_bar_count=history_bar_count,
    )
    registry.register_handler(instance_id, "account.snapshot", service.account_snapshot)
    for operation in _SERVICE_MARKET_OPERATIONS:
        registry.register_handler(
            instance_id,
            operation,
            lambda context, arguments, selected=operation: service.market(
                context, arguments, selected
            ),
        )
    registry.set_ready(True)

    transport = _LocalRequestReplyTransport(
        registry,
        client.principal_ref,
        peer,
        drop_unregister_response=drop_unregister_response,
        drop_register_response=drop_register_response,
    )
    raw_config = {
        "client_config_schema_version": 1,
        "process_role": "strategy_client",
        "registration": {"strategy_ref": sdk_strategy_ref},
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
    parsed_config = parse_client_config(raw_config)
    subscriber = _SourceFakeEventSubscriber(close_failures=subscriber_close_failures)
    transport_factory_calls = []
    subscriber_factory_calls = []

    def transport_factory(profile, selected_peer):
        transport_factory_calls.append((profile, selected_peer))
        return transport

    def subscriber_factory(binding, scope):
        subscriber_factory_calls.append((binding, scope))
        return subscriber

    clock = lambda: datetime(2026, 10, 1, 12, 2, tzinfo=timezone.utc)
    references = SdkRouteReferencesV2(
        strategy=SdkRouteReference(route.references.strategy, sdk_strategy_ref),
        provider=SdkRouteReference(route.references.provider, "synthetic"),
        endpoint=SdkRouteReference(route.references.endpoint, transport_ref),
        account=SdkRouteReference(route.references.account, account_ref),
        credential=SdkRouteReference(route.references.credential, "source-fake-keys"),
        scope=SdkRouteReference(route.references.scope, account_scope_ref),
        product=SdkRouteReference(route.references.product, instrument.product_type),
        data_source=SdkRouteReference(route.references.data_source, market_key.source_ref),
        clock=SdkRouteReference(route.references.clock, "synthetic-clock/exchange-utc"),
    )
    spec = SdkReadOnlySpecV2(
        route=route,
        references=references,
        environment=SdkEnvironmentMapping(route.environment, suffix),
        client_config=parsed_config,
        identity=client,
        peer=peer,
        client_principal_ref=client.principal_ref,
        service_principal_ref=peer.principal_ref,
        account_key=account_key,
        account_resource=resource,
        instrument=instrument,
        subscription_key=market_key,
        account_scope=account_scope,
        market_scope=market_scope,
        stream_scope=stream_scope,
        account_ref=account_ref,
        service_id=service_id,
        instance_id=instance_id,
        account_scope_ref=account_scope_ref,
        market_scope_ref=market_scope_ref,
        transport_ref=transport_ref,
        security_profile_ref="source-fake-keys",
        rpc_endpoint="tcp://127.0.0.1:1",
        market_endpoint="tcp://127.0.0.1:2",
        venue="source-fake",
        product_type=instrument.product_type,
        data_source_ref=market_key.source_ref,
        entitlement_ref=market_key.entitlement_ref,
        market_source_expectation=SdkMarketSourceExpectation(**_SOURCE_EXPECTATION),
        scope_context_field=scope_context_field,
        scope_context_ref=_DOMAIN_REF,
        clock_binding=SdkClockBinding("synthetic-clock/exchange-utc", clock),
        data_name=_DATA_NAME,
        history_start_utc=_BAR_OPEN,
        history_end_utc=history_end_utc or _BAR_CLOSE,
        valuation_currency="USD",
        position_mode="net",
        qty_unit="base_units",
        transport_factory=transport_factory,
        subscriber_factory=subscriber_factory,
    )
    factory = SdkReadOnlyFacadeFactory(spec)
    return {
        "factory": factory,
        "owner": factory.owner,
        "spec": spec,
        "service": service,
        "registry": registry,
        "transport": transport,
        "transport_factory_calls": transport_factory_calls,
        "subscriber_factory_calls": subscriber_factory_calls,
        "subscriber": subscriber,
    }


_SETUP_DIAGNOSTICS: dict[Environment, tuple[str, str, str, str]] = {}


def _runtime_bindings(tmp_path, stacks):
    routes = {
        environment: _route(environment)
        for environment in (Environment.PAPER, Environment.LIVE)
    }

    for environment in (Environment.PAPER, Environment.LIVE):
        stacks[environment] = _make_stack(
            environment,
            tmp_path,
            route=routes[environment],
        )

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
                stacks[environment]["factory"],
            )
            for environment in (Environment.PAPER, Environment.LIVE)
        ),
    )
    return bindings, routes


def _single_stack_bindings(stack, strategy):
    route = stack["spec"].route
    return RuntimeBindingsV2(
        TrustedRegistryV2((route,)),
        (
            RouteBindingV2(
                route,
                strategy,
                (FeedSpec(_DATA_NAME),),
                stack["factory"],
            ),
        ),
    )


def test_real_sdk_facades_run_same_strategy_in_paper_and_live_without_network(
    tmp_path, monkeypatch
):
    _SETUP_DIAGNOSTICS.clear()
    stacks = {}
    bindings, routes = _runtime_bindings(tmp_path, stacks)
    outcomes = []
    factory_ids_after_preflight = set()
    preflight_routes = []
    preflight_before_factory = []
    original_preflight = SdkReadOnlyFacadeFactory.preflight
    original_call = SdkReadOnlyFacadeFactory.__call__

    def observed_preflight(factory, selected_route, *, run_id):
        result = original_preflight(factory, selected_route, run_id=run_id)
        preflight_routes.append((id(factory), selected_route, run_id))
        if result is True:
            factory_ids_after_preflight.add(id(factory))
        return result

    def observed_call(factory):
        preflight_before_factory.append(id(factory) in factory_ids_after_preflight)
        return original_call(factory)

    monkeypatch.setattr(SdkReadOnlyFacadeFactory, "preflight", observed_preflight)
    monkeypatch.setattr(SdkReadOnlyFacadeFactory, "__call__", observed_call)

    for environment in (Environment.PAPER, Environment.LIVE):
        run_id = f"sourcefake-run-{environment.value}"
        session = None
        normal_proxy_close_confirmed = False
        stop_requested = threading.Event()
        timer = None
        try:
            try:
                session = assemble_run(
                    _run_config(routes[environment]),
                    bindings,
                    run_id=run_id,
                )
            except RuntimeAssemblyError as error:
                diagnostic = _SETUP_DIAGNOSTICS.get(environment)
                if error.code == "provider_factory_failed" and diagnostic is not None:
                    phase, exception_type, safe_code, cleanup_state = diagnostic
                    raise AssertionError(
                        "synthetic_provider_setup_failed:"
                        f"{phase}:{exception_type}:{safe_code}:{cleanup_state}"
                    ) from None
                raise

            stack = stacks[environment]
            factory_id = id(stack["factory"])
            assert preflight_routes[-1] == (factory_id, routes[environment], run_id)
            assert preflight_before_factory[-1] is True
            assert sum(item[0] == factory_id for item in preflight_routes) == 1
            assert session.run_id == run_id

            def request_stop_at_deadline(
                stop_requested=stop_requested,
                session=session,
            ):
                stop_requested.set()
                session.request_stop()

            timer = threading.Timer(10.0, request_stop_at_deadline)
            timer.daemon = True
            timer.start()
            (strategy,) = session.run()
            # RunSession.run returns only after its real Store/proxy shutdown checks.
            assert stop_requested.is_set() is False
            stack = stacks[environment]
            owner = stack["owner"]
            assert session._store._service_read_only_stopped is True
            assert owner.proxy.is_connected is False
            normal_proxy_close_confirmed = True
            assert type(owner.runtime) is ServiceClientRuntime
            assert type(owner.account_client) is ServiceAccountClient
            assert type(owner.market_client) is ServiceMarketClient
            assert type(owner.proxy) is ServiceReadProxy
            assert owner.runtime.run_id == run_id
            account_view = owner.runtime.account_binding_view(
                "store.account",
                stack["spec"].account_ref,
                required_operations=_SERVICE_ACCOUNT_OPERATIONS,
            )
            market_view = owner.runtime.market_binding_view(
                "store.market",
                required_operations=_SERVICE_MARKET_OPERATIONS,
            )
            expected_account_scope = replace(
                stack["spec"].account_scope,
                strategy_ref=stack["spec"].identity.actor_ref,
                run_id=run_id,
            )
            expected_market_scope = replace(
                stack["spec"].market_scope,
                strategy_ref=stack["spec"].identity.actor_ref,
                run_id=run_id,
            )
            assert account_view.rpc_scope == expected_account_scope
            assert market_view.rpc_scope == expected_market_scope
            assert account_view.operations == _SERVICE_ACCOUNT_OPERATIONS
            assert market_view.operations == _SERVICE_MARKET_OPERATIONS
            assert owner.market_client.stream_scope.run_id is None
            assert owner.market_client.stream_scope.strategy_ref is None
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
            assert owner.proxy.is_connected is False
            assert stack["service"].market_registrations == set()
            assert stack["subscriber"].closed is True
            assert all(
                scope.run_id is None and scope.strategy_ref is None
                for _binding, scope in stack["subscriber_factory_calls"]
            )
            assert stack["transport"].closed is False
            assert owner.runtime.state == "registered"
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
                    except RuntimeAssemblyError as close_error:
                        # The explicit owner below retries the real facade and
                        # retains all resources if that close remains uncertain.
                        assert close_error.code
                if stack is not None:
                    try:
                        # RunSession closes the per-run proxy boundary but
                        # borrows the factory-owned SDK runtime.
                        stack["owner"].close()
                    except SdkReadOnlyError:
                        pass
                    if stack["owner"].state == "closed":
                        stack["registry"].close()

    assert outcomes == [
        (Environment.PAPER, _SameStrategy, ((True, 100.5),)),
        (Environment.LIVE, _SameStrategy, ((True, 100.5),)),
    ]
    assert type(outcomes[0][1]) is type(outcomes[1][1]) is type
    for environment in (Environment.PAPER, Environment.LIVE):
        stack = stacks[environment]
        owner = stack["owner"]
        assert owner.state == "closed"
        assert owner.runtime.state == "closed"
        assert owner.runtime.cleanup_failures == 0
        assert owner.runtime.unregister_uncertainties == 0
        assert owner.runtime.unknown_registration_count == 0
        assert stack["transport"].closed is True
        with pytest.raises(RuntimeError):
            stack["registry"].set_ready(True)


@pytest.mark.parametrize("run_id", ("", " leading", "bad/path", "é-run", "x" * 129))
def test_sdk_factory_rejects_malformed_run_id_before_runtime_or_transport(tmp_path, run_id):
    stack = _make_stack(Environment.PAPER, tmp_path)

    with pytest.raises(SdkReadOnlyError) as error:
        stack["factory"].preflight(stack["spec"].route, run_id=run_id)

    assert error.value.code == "sdk_run_identity_invalid"
    assert str(error.value) == "sdk_run_identity_invalid"
    assert stack["owner"].runtime is None
    assert stack["transport_factory_calls"] == []
    assert stack["transport"].operations == []
    stack["registry"].close()


def test_sdk_factory_requires_explicit_runtime_run_id_keyword_before_io(monkeypatch, tmp_path):
    stack = _make_stack(Environment.PAPER, tmp_path)

    def legacy_runtime_init(
        runtime,
        config,
        *,
        identity,
        scope_resolver,
        peer_resolver,
        transport_factory,
        client_session_id=None,
        requested_operations=None,
        monotonic_clock=None,
    ):
        raise AssertionError("legacy runtime constructor must not be called")

    monkeypatch.setattr(ServiceClientRuntime, "__init__", legacy_runtime_init)
    with pytest.raises(SdkReadOnlyError) as error:
        stack["factory"].preflight(stack["spec"].route, run_id="selected-run")

    assert error.value.code == "sdk_runtime_run_identity_unsupported"
    assert stack["owner"].runtime is None
    assert stack["transport_factory_calls"] == []
    assert stack["transport"].operations == []
    stack["registry"].close()


@pytest.mark.parametrize(
    "signature_case",
    (
        "account_arguments_keyword_only",
        "market_mount_keyword_only",
        "account_arguments_reordered",
        "required_operations_positional_only",
    ),
)
def test_sdk_factory_rejects_incompatible_binding_view_call_signature_before_io(
    monkeypatch, tmp_path, signature_case
):
    stack = _make_stack(Environment.PAPER, tmp_path)

    if signature_case == "account_arguments_keyword_only":

        def incompatible_view(runtime, *, mount, account_ref, required_operations):
            raise AssertionError("preflight must inspect without calling the view")

        monkeypatch.setattr(ServiceClientRuntime, "account_binding_view", incompatible_view)
    elif signature_case == "market_mount_keyword_only":

        def incompatible_view(runtime, *, mount, required_operations):
            raise AssertionError("preflight must inspect without calling the view")

        monkeypatch.setattr(ServiceClientRuntime, "market_binding_view", incompatible_view)
    elif signature_case == "account_arguments_reordered":

        def incompatible_view(runtime, account_ref, mount, *, required_operations):
            raise AssertionError("preflight must inspect without calling the view")

        monkeypatch.setattr(ServiceClientRuntime, "account_binding_view", incompatible_view)
    else:

        def incompatible_view(runtime, mount, account_ref, required_operations, /):
            raise AssertionError("preflight must inspect without calling the view")

        monkeypatch.setattr(ServiceClientRuntime, "account_binding_view", incompatible_view)

    with pytest.raises(SdkReadOnlyError) as error:
        stack["factory"].preflight(stack["spec"].route, run_id="selected-run")

    assert error.value.code == "sdk_registered_binding_views_unsupported"
    assert error.value.__context__ is None
    assert stack["owner"].runtime is None
    assert stack["transport_factory_calls"] == []
    assert stack["transport"].operations == []
    assert stack["subscriber_factory_calls"] == []
    assert stack["subscriber"].close_calls == 0
    stack["registry"].close()


def test_sdk_factory_requires_identity_and_refuses_cross_run_rebinding_before_io(tmp_path):
    stack = _make_stack(Environment.PAPER, tmp_path)
    with pytest.raises(SdkReadOnlyError) as missing:
        stack["factory"].build()

    assert missing.value.code == "sdk_run_identity_required"
    assert stack["owner"].runtime is None
    assert stack["transport_factory_calls"] == []

    assert stack["factory"].preflight(
        stack["spec"].route,
        run_id="first-run",
    ) is True
    route = stack["spec"].route
    bindings = RuntimeBindingsV2(
        TrustedRegistryV2((route,)),
        (RouteBindingV2(route, _SameStrategy, (FeedSpec(_DATA_NAME),), stack["factory"]),),
    )

    with pytest.raises(RuntimeAssemblyError) as mismatch:
        assemble_run(_run_config(route), bindings, run_id="second-run")

    assert mismatch.value.code == "provider_binding_preflight_failed"
    assert mismatch.value.__context__ is None
    assert stack["owner"].runtime is None
    assert stack["transport_factory_calls"] == []
    assert stack["transport"].operations == []
    stack["registry"].close()


def test_sdk_factory_rejects_runtime_run_id_mismatch_before_transport(monkeypatch, tmp_path):
    stack = _make_stack(Environment.PAPER, tmp_path)
    original_init = ServiceClientRuntime.__init__

    def mismatched_init(
        runtime,
        config,
        *,
        identity,
        scope_resolver,
        peer_resolver,
        transport_factory,
        client_session_id=None,
        run_id=None,
        requested_operations=None,
        monotonic_clock=time.monotonic,
    ):
        original_init(
            runtime,
            config,
            identity=identity,
            scope_resolver=scope_resolver,
            peer_resolver=peer_resolver,
            transport_factory=transport_factory,
            client_session_id=client_session_id,
            run_id=run_id,
            requested_operations=requested_operations,
            monotonic_clock=monotonic_clock,
        )
        runtime.run_id = "different-run"

    monkeypatch.setattr(ServiceClientRuntime, "__init__", mismatched_init)

    with pytest.raises(SdkReadOnlyError) as error:
        stack["factory"].build(run_id="selected-run")

    assert error.value.code == "sdk_runtime_run_id_mismatch"
    assert stack["owner"].runtime is not None
    assert stack["owner"].state == "closed"
    assert stack["owner"].runtime.unknown_registration_count == 0
    assert stack["transport_factory_calls"] == []
    assert stack["transport"].operations == []
    stack["registry"].close()


@pytest.mark.parametrize("mismatch", ("account_run_id", "market_operations"))
def test_registered_binding_mismatch_retains_uncertain_owner_and_registration(
    monkeypatch, tmp_path, mismatch
):
    stack = _make_stack(
        Environment.PAPER,
        tmp_path,
        drop_unregister_response=True,
    )
    if mismatch == "account_run_id":
        original_view = ServiceClientRuntime.account_binding_view

        def mismatched_view(runtime, mount, account_ref, *, required_operations):
            view = original_view(
                runtime,
                mount,
                account_ref,
                required_operations=required_operations,
            )
            return replace(
                view,
                rpc_scope=replace(view.rpc_scope, run_id="different-run"),
            )

        monkeypatch.setattr(ServiceClientRuntime, "account_binding_view", mismatched_view)
        expected_code = "sdk_registered_binding_view_mismatch"
    else:
        original_view = ServiceClientRuntime.market_binding_view

        def mismatched_view(runtime, mount, *, required_operations):
            view = original_view(
                runtime,
                mount,
                required_operations=required_operations,
            )
            return replace(view, operations=frozenset({"market.subscribe"}))

        monkeypatch.setattr(ServiceClientRuntime, "market_binding_view", mismatched_view)
        expected_code = "sdk_registered_binding_view_mismatch"

    with pytest.raises(SdkReadOnlyError) as error:
        stack["factory"].build(run_id="selected-run")

    owner = stack["owner"]
    assert error.value.code == expected_code
    assert owner.retained is True
    assert owner.state == "remote_uncertain"
    assert owner.runtime.unregister_uncertainties > 0
    assert owner.runtime.unknown_registration_count == 0
    assert stack["transport"].operations.count("service.register") == 2
    stack["registry"].close()


def test_lost_register_response_keeps_unknown_registration_and_owner_resources(tmp_path):
    stack = _make_stack(Environment.PAPER, tmp_path, drop_register_response=True)

    with pytest.raises(SdkReadOnlyError) as error:
        stack["factory"].build(run_id="selected-run")

    owner = stack["owner"]
    assert error.value.code == "sdk_facade_construction_failed"
    assert owner.retained is True
    assert owner.state == "remote_uncertain"
    assert owner.runtime.unknown_registration_count == 1
    assert owner.runtime.unregister_uncertainties == 0
    assert stack["transport"].operations.count("service.register") == 1
    stack["registry"].close()


def test_failed_proxy_close_is_retained_until_owner_retry(tmp_path):
    stack = _make_stack(
        Environment.PAPER,
        tmp_path,
        subscriber_close_failures=1,
    )
    owner = stack["factory"].build(run_id="sourcefake-run")
    owner.market_client.start()
    assert stack["service"].market_registrations

    # A false connection flag during ServiceReadProxy's closing state is not a
    # close receipt. The real proxy raised, so runtime and Registry stay owned.
    with pytest.raises(SdkReadOnlyError) as first_close:
        owner.close()
    assert first_close.value.code == "sdk_facade_close_failed"
    assert owner.retained is True
    assert owner.state == "close_failed"
    assert owner.runtime.state == "registered"
    assert stack["registry"].ready is True
    assert stack["transport"].closed is False
    assert stack["subscriber"].close_calls == 1
    assert stack["subscriber"].closed is False

    owner.close()
    assert owner.retained is False
    assert owner.state == "closed"
    assert owner.proxy.is_connected is False
    assert owner.runtime.state == "closed"
    assert stack["transport"].closed is True
    assert stack["subscriber"].close_calls == 2
    assert stack["subscriber"].closed is True
    stack["registry"].close()
    with pytest.raises(RuntimeError):
        stack["registry"].set_ready(True)


def test_factory_requires_sdk_market_source_descriptor(tmp_path):
    stack = _make_stack(Environment.PAPER, tmp_path)
    assert stack["factory"].preflight(
        stack["spec"].route, run_id="sourcefake-run"
    ) is True
    owner = stack["factory"].build()
    try:
        owner.market_client.start()
        descriptor = owner.market_client.source_descriptor
        expected = owner.market_client._expected_source_identity
        assert type(expected) is ExpectedMarketSourceIdentity
        assert {
            "source": expected.source,
            "clock_domain_id": expected.clock_domain_id,
            "clock_source_session_id": expected.clock_source_session_id,
            "clock_source": expected.clock_source,
        } == _SOURCE_EXPECTATION
        assert type(descriptor) is MarketSourceDescriptor
        assert descriptor.key == owner.market_client.key
        assert {
            "source": descriptor.source,
            "clock_domain_id": descriptor.clock_domain_id,
            "clock_source_session_id": descriptor.clock_source_session_id,
            "clock_source": descriptor.clock_source,
        } == _SOURCE_EXPECTATION
    finally:
        owner.close()
        stack["registry"].close()


@pytest.mark.parametrize(
    "invalid_expectation",
    (None, "whitespace", "wrong_type"),
    ids=("missing", "whitespace", "wrong_type"),
)
def test_source_expectation_must_be_explicit_and_valid_before_io(tmp_path, invalid_expectation):
    stack = _make_stack(Environment.PAPER, tmp_path)
    expectation = stack["spec"].market_source_expectation
    if invalid_expectation == "whitespace":
        expectation = replace(expectation, clock_source_session_id=" session ")
    elif invalid_expectation == "wrong_type":
        expectation = {"source": "received-descriptor-must-not-be-used"}
    else:
        expectation = None
    factory = SdkReadOnlyFacadeFactory(
        replace(stack["spec"], market_source_expectation=expectation)
    )

    with pytest.raises(SdkReadOnlyError) as error:
        factory.build(run_id="sourcefake-run")

    assert error.value.code == "invalid_sdk_market_source_expectation"
    assert error.value.__context__ is None
    assert factory.owner.runtime is None
    assert stack["transport_factory_calls"] == []
    assert stack["subscriber_factory_calls"] == []
    assert stack["transport"].operations == []
    assert stack["service"].operations == []
    stack["registry"].close()


@pytest.mark.parametrize("field", tuple(_SOURCE_EXPECTATION))
def test_each_source_identity_mismatch_blocks_first_bar_and_history(tmp_path, field):
    observations = []
    stack = _make_stack(
        Environment.PAPER,
        tmp_path,
        source_identity_overrides=({field: f"unexpected-{field}"},),
    )

    class NoBarStrategy(bt.Strategy):
        params = (("enabled", False),)

        def next(self):
            observations.append(float(self.data.close[0]))

    session = assemble_run(
        _run_config(stack["spec"].route),
        _single_stack_bindings(stack, NoBarStrategy),
        run_id="sourcefake-run",
    )
    try:
        with pytest.raises(ServiceMarketError) as run_error:
            session.run()

        owner = stack["owner"]
        assert run_error.value.code == "SOURCE_IDENTITY_MISMATCH"
        assert observations == []
        assert stack["service"].history_calls == 0
        assert stack["service"].subscribe_calls == 1
        assert "market.subscribe" in stack["service"].operations
        assert "market.history" not in stack["service"].operations
        assert "market.unsubscribe" in stack["service"].operations
        assert stack["service"].market_registrations == set()
        assert stack["subscriber"].closed is True

        owner.close()
        assert owner.state == "closed"
        assert owner.runtime.state == "closed"
        assert owner.runtime.cleanup_failures == 0
        assert owner.runtime.unregister_uncertainties == 0
        assert owner.runtime.unknown_registration_count == 0
        assert stack["transport"].closed is True
    finally:
        if stack["owner"].state != "closed":
            try:
                stack["owner"].close()
            except SdkReadOnlyError:
                pass
        stack["registry"].close()


def test_source_mismatch_cleanup_uncertainty_retains_owner_resources(tmp_path):
    observations = []
    stack = _make_stack(
        Environment.PAPER,
        tmp_path,
        source_identity_overrides=({"source": "unexpected-source"},),
        drop_unregister_response=True,
    )

    class NoBarStrategy(bt.Strategy):
        params = (("enabled", False),)

        def next(self):
            observations.append(float(self.data.close[0]))

    session = assemble_run(
        _run_config(stack["spec"].route),
        _single_stack_bindings(stack, NoBarStrategy),
        run_id="sourcefake-run",
    )
    try:
        with pytest.raises(ServiceMarketError) as run_error:
            session.run()

        owner = stack["owner"]
        assert run_error.value.code == "SOURCE_IDENTITY_MISMATCH"
        assert observations == []
        assert stack["service"].history_calls == 0
        assert stack["service"].market_registrations == set()
        assert stack["subscriber"].closed is True

        with pytest.raises(SdkReadOnlyError) as close_error:
            owner.close()

        assert close_error.value.code == "sdk_runtime_cleanup_unconfirmed"
        assert owner.retained is True
        assert owner.state == "remote_uncertain"
        assert owner.runtime.state == "closed"
        assert owner.runtime.unregister_uncertainties > 0
        assert stack["transport"].closed is True
    finally:
        stack["registry"].close()


def test_recovery_source_mismatch_cannot_deliver_another_strategy_bar(tmp_path):
    observations = []
    recovery = {}
    stack = _make_stack(
        Environment.LIVE,
        tmp_path,
        source_identity_overrides=(
            None,
            {"clock_source_session_id": "unexpected-recovered-session"},
        ),
        history_bar_count=2,
        history_end_utc=_BAR_TWO_CLOSE,
    )

    class RecoverOnFirstBarStrategy(bt.Strategy):
        params = (("enabled", False),)

        def next(self):
            observations.append(float(self.data.close[0]))
            if len(observations) == 1:
                try:
                    market_client.recover()
                except ServiceMarketError as error:
                    recovery["code"] = error.code
                    recovery["descriptor"] = market_client.source_descriptor
                    try:
                        market_client.poll()
                    except ServiceMarketError as poll_error:
                        recovery["poll_code"] = poll_error.code
                    raise

    session = assemble_run(
        _run_config(stack["spec"].route),
        _single_stack_bindings(stack, RecoverOnFirstBarStrategy),
        run_id="sourcefake-run",
    )
    market_client = stack["owner"].market_client
    try:
        with pytest.raises(ServiceMarketError) as run_error:
            session.run()

        assert run_error.value.code == "SOURCE_IDENTITY_MISMATCH"
        assert observations == [100.5]
        assert recovery == {
            "code": "SOURCE_IDENTITY_MISMATCH",
            "descriptor": None,
            "poll_code": "CLIENT_NOT_STARTED",
        }
        assert stack["service"].subscribe_calls == 2
        assert stack["service"].history_calls == 1
        assert stack["service"].operations.count("market.history") == 1
        assert stack["owner"].market_client.source_descriptor is None
        assert stack["service"].market_registrations == set()
        assert stack["subscriber"].closed is True

        stack["owner"].close()
        assert stack["owner"].state == "closed"
        assert stack["owner"].runtime.state == "closed"
        assert stack["owner"].runtime.cleanup_failures == 0
        assert stack["owner"].runtime.unregister_uncertainties == 0
        assert stack["owner"].runtime.unknown_registration_count == 0
    finally:
        if stack["owner"].state != "closed":
            try:
                stack["owner"].close()
            except SdkReadOnlyError:
                pass
        stack["registry"].close()


def test_expected_source_identity_batch_start_does_not_fallback_to_singleton(tmp_path):
    stack = _make_stack(Environment.PAPER, tmp_path)
    owner = stack["factory"].build(run_id="sourcefake-run")
    try:
        with pytest.raises(ServiceMarketError) as error:
            ServiceMarketClient.start_many((owner.market_client,))

        assert error.value.code == "EXPECTED_SOURCE_REQUIRES_BATCH_BOOTSTRAP"
        assert stack["service"].operations == []
        assert stack["subscriber_factory_calls"] == []
        assert stack["subscriber"].close_calls == 0
        owner.close()
        assert owner.state == "closed"
        assert owner.runtime.state == "closed"
    finally:
        if owner.state != "closed":
            try:
                owner.close()
            except SdkReadOnlyError:
                pass
        stack["registry"].close()


def test_route_mapping_mismatch_is_rejected_before_transport_factory_or_start(tmp_path):
    stack = _make_stack(Environment.PAPER, tmp_path)
    spec = stack["spec"]
    bad_provider_pair = replace(
        spec.references.provider,
        route_ref="provider/not-the-selected-route",
    )
    bad_spec = replace(
        spec,
        references=replace(spec.references, provider=bad_provider_pair),
    )
    factory = SdkReadOnlyFacadeFactory(bad_spec)

    with pytest.raises(SdkReadOnlyError) as error:
        factory.build(run_id="sourcefake-run")

    assert error.value.code == "route_reference_mapping_mismatch"
    assert error.value.__context__ is None
    assert factory.owner.runtime is None
    assert stack["transport_factory_calls"] == []
    assert stack["transport"].operations == []
    stack["registry"].close()


def test_environment_mapping_mismatch_is_rejected_before_transport_factory(tmp_path):
    stack = _make_stack(Environment.PAPER, tmp_path)
    bad_spec = replace(
        stack["spec"],
        environment=SdkEnvironmentMapping(Environment.PAPER, "live"),
    )
    factory = SdkReadOnlyFacadeFactory(bad_spec)

    with pytest.raises(SdkReadOnlyError) as error:
        factory.build(run_id="sourcefake-run")

    assert error.value.code == "sdk_route_binding_mismatch"
    assert error.value.__context__ is None
    assert factory.owner.runtime is None
    assert stack["transport_factory_calls"] == []
    assert stack["transport"].operations == []
    stack["registry"].close()


def test_clock_reference_and_callable_mismatch_is_rejected_before_io(tmp_path):
    stack = _make_stack(Environment.PAPER, tmp_path)
    spec = stack["spec"]
    bad_spec = replace(
        spec,
        clock_binding=SdkClockBinding("another-clock", spec.clock_binding.read),
    )
    factory = SdkReadOnlyFacadeFactory(bad_spec)

    with pytest.raises(SdkReadOnlyError) as error:
        factory.build(run_id="sourcefake-run")

    assert error.value.code == "sdk_route_binding_mismatch"
    assert error.value.__context__ is None
    assert factory.owner.runtime is None
    assert stack["transport_factory_calls"] == []
    assert stack["transport"].operations == []
    stack["registry"].close()


def test_scope_with_unmapped_run_dimension_is_rejected_before_io(tmp_path):
    stack = _make_stack(Environment.PAPER, tmp_path)
    spec = stack["spec"]
    bad_spec = replace(
        spec,
        market_scope=replace(spec.market_scope, run_id="unexpected-run"),
    )
    factory = SdkReadOnlyFacadeFactory(bad_spec)

    with pytest.raises(SdkReadOnlyError) as error:
        factory.build(run_id="sourcefake-run")

    assert error.value.code == "sdk_scope_or_market_mapping_mismatch"
    assert error.value.__context__ is None
    assert factory.owner.runtime is None
    assert stack["transport_factory_calls"] == []
    assert stack["transport"].operations == []
    stack["registry"].close()


def test_assemble_run_preflights_factory_against_selected_route(tmp_path):
    selected_route = _route(Environment.PAPER)
    mismatched_stack = _make_stack(Environment.LIVE, tmp_path)
    bindings = RuntimeBindingsV2(
        TrustedRegistryV2((selected_route,)),
        (
            RouteBindingV2(
                selected_route,
                _SameStrategy,
                (FeedSpec(_DATA_NAME),),
                mismatched_stack["factory"],
            ),
        ),
    )

    with pytest.raises(RuntimeAssemblyError) as error:
        assemble_run(_run_config(selected_route), bindings, run_id="sourcefake-run")

    assert error.value.code == "provider_binding_preflight_failed"
    assert error.value.__context__ is None
    assert mismatched_stack["owner"].runtime is None
    assert mismatched_stack["transport_factory_calls"] == []
    assert mismatched_stack["transport"].operations == []
    mismatched_stack["registry"].close()


@pytest.mark.parametrize("failure_phase", ("lookup", "call"))
@pytest.mark.parametrize("interrupt_type", (GeneratorExit, KeyboardInterrupt, SystemExit))
def test_assemble_run_propagates_preflight_process_control_signals(
    tmp_path, failure_phase, interrupt_type
):
    route = _route(Environment.PAPER)
    marker = interrupt_type("synthetic preflight control")

    class FactoryWithoutInterruptingHook:
        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            raise AssertionError("provider factory ran after interrupted preflight")

    if failure_phase == "lookup":

        class InterruptingLookupFactory(FactoryWithoutInterruptingHook):
            @property
            def preflight(self):
                raise marker

        factory = InterruptingLookupFactory()
    else:

        class InterruptingCallFactory(FactoryWithoutInterruptingHook):
            def preflight(self, selected_route, *, run_id):
                assert selected_route == route
                assert run_id == "sourcefake-run"
                raise marker

        factory = InterruptingCallFactory()

    bindings = RuntimeBindingsV2(
        TrustedRegistryV2((route,)),
        (RouteBindingV2(route, _SameStrategy, (FeedSpec(_DATA_NAME),), factory),),
    )

    with pytest.raises(interrupt_type) as error:
        assemble_run(_run_config(route), bindings, run_id="sourcefake-run")

    assert error.value is marker
    assert error.value.__context__ is None
    assert factory.calls == 0


def test_sdk_factory_cleans_then_propagates_transport_keyboard_interrupt(tmp_path):
    stack = _make_stack(Environment.PAPER, tmp_path)
    marker = KeyboardInterrupt("synthetic transport interrupt")

    def interrupted_transport_factory(_profile, _peer):
        raise marker

    spec = replace(stack["spec"], transport_factory=interrupted_transport_factory)
    factory = SdkReadOnlyFacadeFactory(spec)

    with pytest.raises(KeyboardInterrupt) as error:
        factory.build(run_id="sourcefake-run")

    assert error.value is marker
    assert error.value.__context__ is None
    assert factory.owner.state == "closed"
    assert factory.owner.retained is False
    assert factory.owner.runtime.state == "closed"
    assert factory.owner.runtime.cleanup_failures == 0
    assert factory.owner.runtime.unregister_uncertainties == 0
    assert factory.owner.runtime.unknown_registration_count == 0
    assert stack["transport_factory_calls"] == []
    assert stack["transport"].operations == []
    stack["registry"].close()


def test_sdk_mapping_repr_redacts_refs_endpoints_and_factory_objects(tmp_path):
    stack = _make_stack(Environment.PAPER, tmp_path)
    spec = stack["spec"]
    joined = " ".join(
        (
            repr(spec),
            repr(spec.references),
            repr(spec.references.credential),
            repr(spec.environment),
            repr(spec.clock_binding),
        )
    )
    for private_marker in (
        "source-fake-keys",
        "synthetic-account-paper",
        "tcp://127.0.0.1:1",
        "synthetic-clock/exchange-utc",
    ):
        assert private_marker not in joined
    stack["registry"].close()


def _assert_ambient_exception_is_suppressed(error, *private_markers):
    assert error.__suppress_context__ is True
    rendered = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    for marker in private_markers:
        assert marker not in rendered


def test_sdk_import_boundary_suppresses_ambient_exception(monkeypatch, tmp_path):
    stack = _make_stack(Environment.PAPER, tmp_path)
    original_import = builtins.__import__

    def fail_runtime_import(name, *args, **kwargs):
        if name == "bt_api_py.service_runtime":
            raise ImportError("sdk-import-private-token")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_runtime_import)
    try:
        raise RuntimeError("ambient-private-token")
    except RuntimeError:
        with pytest.raises(SdkReadOnlyError) as error:
            stack["factory"].preflight(
                stack["spec"].route, run_id="sourcefake-run"
            )

    assert error.value.code == "sdk_read_dependencies_unavailable"
    _assert_ambient_exception_is_suppressed(
        error.value,
        "ambient-private-token",
        "sdk-import-private-token",
    )
    assert stack["factory"].owner.runtime is None
    assert stack["transport_factory_calls"] == []
    stack["registry"].close()


@pytest.mark.parametrize(
    "legacy_shape",
    ("missing_identity_type", "missing_constructor_keyword"),
    ids=("missing-identity-type", "missing-constructor-keyword"),
)
def test_legacy_sdk_source_identity_api_fails_before_io_and_redacts(
    monkeypatch,
    tmp_path,
    legacy_shape,
):
    stack = _make_stack(Environment.PAPER, tmp_path)
    synthetic_market_module = ModuleType("bt_api_py.service_market")

    if legacy_shape == "missing_identity_type":
        synthetic_market_module.ServiceMarketClient = ServiceMarketClient
    else:
        synthetic_market_module.ExpectedMarketSourceIdentity = ExpectedMarketSourceIdentity

        class LegacyServiceMarketClient:
            def __init__(self, runtime, mount, key, *, subscriber_factory=None, stream_scope):
                pass

        synthetic_market_module.ServiceMarketClient = LegacyServiceMarketClient

    monkeypatch.setitem(sys.modules, "bt_api_py.service_market", synthetic_market_module)
    try:
        raise RuntimeError("ambient-private-token")
    except RuntimeError:
        with pytest.raises(SdkReadOnlyError) as error:
            stack["factory"].preflight(
                stack["spec"].route, run_id="sourcefake-run"
            )

    assert error.value.code == "sdk_market_source_identity_unsupported"
    assert str(error.value) == "sdk_market_source_identity_unsupported"
    _assert_ambient_exception_is_suppressed(error.value, "ambient-private-token")

    route = stack["spec"].route
    bindings = RuntimeBindingsV2(
        TrustedRegistryV2((route,)),
        (
            RouteBindingV2(
                route,
                _SameStrategy,
                (FeedSpec(_DATA_NAME),),
                stack["factory"],
            ),
        ),
    )
    with pytest.raises(RuntimeAssemblyError) as assembly_error:
        assemble_run(_run_config(route), bindings, run_id="sourcefake-run")

    assert assembly_error.value.code == "provider_binding_preflight_failed"
    assert stack["factory"].owner.runtime is None
    assert stack["transport_factory_calls"] == []
    assert stack["subscriber_factory_calls"] == []
    assert stack["transport"].operations == []
    assert stack["subscriber"].close_calls == 0
    stack["registry"].close()


def test_kwargs_only_legacy_market_client_fails_before_factory_or_io(monkeypatch, tmp_path):
    stack = _make_stack(Environment.PAPER, tmp_path)
    synthetic_market_module = ModuleType("bt_api_py.service_market")
    synthetic_market_module.ExpectedMarketSourceIdentity = ExpectedMarketSourceIdentity

    class KwargsOnlyLegacyServiceMarketClient:
        def __init__(self, *args, **kwargs):
            pass

    synthetic_market_module.ServiceMarketClient = KwargsOnlyLegacyServiceMarketClient
    monkeypatch.setitem(sys.modules, "bt_api_py.service_market", synthetic_market_module)

    with pytest.raises(SdkReadOnlyError) as error:
        stack["factory"].preflight(stack["spec"].route, run_id="sourcefake-run")

    assert error.value.code == "sdk_market_source_identity_unsupported"
    assert stack["factory"].owner.runtime is None
    assert stack["transport_factory_calls"] == []
    assert stack["subscriber_factory_calls"] == []
    assert stack["transport"].operations == []
    assert stack["subscriber"].close_calls == 0
    stack["registry"].close()


def test_sdk_config_boundary_suppresses_ambient_exception(monkeypatch, tmp_path):
    stack = _make_stack(Environment.PAPER, tmp_path)

    def fail_public_projection(_config):
        raise ValueError("config-private-token")

    monkeypatch.setattr(
        type(stack["spec"].client_config),
        "to_public_dict",
        fail_public_projection,
    )
    try:
        raise RuntimeError("ambient-private-token")
    except RuntimeError:
        with pytest.raises(SdkReadOnlyError) as error:
            stack["factory"].preflight(
                stack["spec"].route, run_id="sourcefake-run"
            )

    assert error.value.code == "client_config_unreadable"
    _assert_ambient_exception_is_suppressed(
        error.value,
        "ambient-private-token",
        "config-private-token",
    )
    assert stack["factory"].owner.runtime is None
    assert stack["transport_factory_calls"] == []
    stack["registry"].close()


def test_sdk_build_boundary_suppresses_ambient_exception(tmp_path):
    stack = _make_stack(Environment.PAPER, tmp_path)

    def fail_transport_factory(_profile, _peer):
        raise RuntimeError("transport-private-token")

    factory = SdkReadOnlyFacadeFactory(
        replace(stack["spec"], transport_factory=fail_transport_factory)
    )
    try:
        raise RuntimeError("ambient-private-token")
    except RuntimeError:
        with pytest.raises(SdkReadOnlyError) as error:
            factory.build(run_id="sourcefake-run")

    assert error.value.code == "sdk_facade_construction_failed"
    _assert_ambient_exception_is_suppressed(
        error.value,
        "ambient-private-token",
        "transport-private-token",
    )
    assert factory.owner.state == "closed"
    assert factory.owner.runtime.cleanup_failures == 0
    assert factory.owner.runtime.unregister_uncertainties == 0
    assert factory.owner.runtime.unknown_registration_count == 0
    stack["registry"].close()


@pytest.mark.parametrize("failure_phase", ("preflight", "provider_factory"))
def test_assembly_boundary_suppresses_ambient_exception(failure_phase):
    route = _route(Environment.PAPER)

    class FailingFactory:
        def preflight(self, selected_route, *, run_id):
            assert selected_route == route
            assert run_id == "sourcefake-run"
            if failure_phase == "preflight":
                raise RuntimeError("preflight-private-token")
            return True

        def __call__(self):
            raise RuntimeError("factory-private-token")

    bindings = RuntimeBindingsV2(
        TrustedRegistryV2((route,)),
        (
            RouteBindingV2(
                route,
                _SameStrategy,
                (FeedSpec(_DATA_NAME),),
                FailingFactory(),
            ),
        ),
    )
    try:
        raise RuntimeError("ambient-private-token")
    except RuntimeError:
        with pytest.raises(RuntimeAssemblyError) as error:
            assemble_run(_run_config(route), bindings, run_id="sourcefake-run")

    expected_code = (
        "provider_binding_preflight_failed"
        if failure_phase == "preflight"
        else "provider_factory_failed"
    )
    private_marker = (
        "preflight-private-token"
        if failure_phase == "preflight"
        else "factory-private-token"
    )
    assert error.value.code == expected_code
    _assert_ambient_exception_is_suppressed(
        error.value,
        "ambient-private-token",
        private_marker,
    )


def test_remote_unregister_uncertainty_keeps_owner_unconfirmed(tmp_path):
    stack = _make_stack(
        Environment.PAPER,
        tmp_path,
        drop_unregister_response=True,
    )
    owner = stack["factory"].build(run_id="sourcefake-run")

    with pytest.raises(SdkReadOnlyError) as first_close:
        owner.close()

    assert first_close.value.code == "sdk_runtime_cleanup_unconfirmed"
    assert owner.retained is True
    assert owner.runtime.state == "closed"
    assert owner.runtime.cleanup_failures > 0
    assert owner.runtime.unregister_uncertainties > 0
    assert owner.runtime.unknown_registration_count == 0
    assert stack["transport"].closed is True

    # The owner retains the whole group, but does not claim a retry can recover
    # a remote result after the SDK has already closed its transport.
    with pytest.raises(SdkReadOnlyError) as retry:
        owner.close()
    assert retry.value.code == "sdk_remote_registration_uncertain"
    assert owner.retained is True
    assert owner.state == "remote_uncertain"
    stack["registry"].close()
