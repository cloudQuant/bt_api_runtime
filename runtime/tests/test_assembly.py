"""Source-fake integration of the real read-only proxy and Backtrader engine."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import backtrader as bt
import pytest
from backtrader.brokers.btapibroker import BtApiBroker
from backtrader.feeds.btapifeed import BtApiFeed
from backtrader.stores.btapistore import BtApiStore
from backtrader.stores.service_read_proxy import (
    ServiceCtpTickBinding,
    ServiceFeedBinding,
    ServiceOrderBookBinding,
    ServiceReadProxy,
)
from bt_api_base.account_protocol import (
    AccountBalance,
    AccountKey,
    AccountObservation,
    AccountResourceKey,
    AccountValuation,
)
from bt_api_base.market_history_protocol import MarketBar, MarketHistoryPage
from bt_api_base.market_protocol import MarketInstrumentKey, MarketSubscriptionKey
from bt_api_base.service_protocol import WireScope
from bt_api_run_contract import (
    Environment,
    References,
    ReferencesV2,
    RunConfigError,
    TrustedRegistry,
    TrustedRegistryV2,
    TrustedRoute,
    TrustedRouteV2,
    ValidatedRunConfigV2,
    parse_config,
)

from bt_api_runtime import (
    FeedSpec,
    RouteBinding,
    RouteBindingV2,
    RuntimeAssemblyError,
    RuntimeBindings,
    RuntimeBindingsV2,
    assemble_run,
)

_NOW = datetime(2026, 10, 1, 12, 3, tzinfo=timezone.utc)
_OBSERVED_AT = "2026-10-01T12:03:00Z"


class _AccountFacade:
    """Fake outer SDK account client; it never reports real identity or authority."""

    def __init__(self, account, resource):
        self.expected_account = account
        self.required_coverage = (resource,)
        self.require_open_orders = True
        self.refresh_calls = 0
        self.close_calls = 0
        self.write_calls = []
        self.fail_refresh = False
        self.fail_snapshot_then_refresh = False
        self.snapshot_calls = 0
        self._observation = AccountObservation(
            account_key=account,
            observation_id="fixture-observation",
            coverage=(resource,),
            balances=(AccountBalance(resource, "USD", "10", "10", "0", _OBSERVED_AT),),
            positions=(),
            valuation=AccountValuation("USD", "10", "10", _OBSERVED_AT),
            started_at_utc="2026-10-01T12:02:00Z",
            completed_at_utc=_OBSERVED_AT,
            consistency="provider_atomic",
            balances_complete=True,
            positions_complete=True,
            open_orders=(),
            open_orders_complete=True,
        )

    def refresh(self):
        self.refresh_calls += 1
        if self.fail_refresh:
            raise RuntimeError("fixture account refresh failure")
        return SimpleNamespace(observation=self._observation)

    def snapshot(self):
        self.snapshot_calls += 1
        if self.fail_snapshot_then_refresh:
            self.fail_refresh = True
            raise RuntimeError("fixture snapshot failure after connect")
        return SimpleNamespace(observation=self._observation)

    def close(self):
        self.close_calls += 1


class _MarketFacade:
    """Fake outer SDK bar client; real ServiceReadProxy validates its results."""

    def __init__(self, key, scope, *, empty_history=False, fail_history=False, fail_start=False):
        self.key = key
        self.stream_scope = scope
        self.healthy = True
        self.recovery_required = False
        self.empty_history = empty_history
        self.fail_history = fail_history
        self.fail_start = fail_start
        self.start_calls = 0
        self.history_calls = 0
        self.poll_calls = 0
        self.on_poll = None
        self.close_calls = 0
        self.write_calls = []

    def start(self):
        self.start_calls += 1
        if self.fail_start:
            raise RuntimeError("fixture market start failure")
        return SimpleNamespace(
            snapshot=None,
            snapshot_kind="latest_event",
            snapshot_complete=False,
            full_baseline_available=False,
            stream_id=self.key.stream_id,
            snapshot_epoch="fixture-epoch",
            snapshot_sequence=0,
            cursor_epoch="fixture-epoch",
            cursor_sequence=0,
            resync_required=False,
        )

    def history(self, request):
        self.history_calls += 1
        if self.fail_history:
            raise RuntimeError("fixture history failure")
        if self.empty_history:
            return MarketHistoryPage(
                request=request,
                bars=(),
                complete=True,
                next_start_time_utc=None,
                observed_at_utc=_OBSERVED_AT,
            )
        bar = MarketBar(
            open_time_utc="2026-10-01T12:00:00Z",
            close_time_utc="2026-10-01T12:01:00Z",
            open="100",
            high="101",
            low="99",
            close="100.5",
            volume="2",
            open_interest=None,
        )
        return MarketHistoryPage(
            request=request,
            bars=(bar,),
            complete=True,
            next_start_time_utc=None,
            observed_at_utc=_OBSERVED_AT,
        )

    def poll(self, *, timeout_ms=0):
        self.poll_calls += 1
        if callable(self.on_poll):
            self.on_poll()

    def close(self):
        self.close_calls += 1


class _ObserveAndAttemptBuy(bt.Strategy):
    def __init__(self):
        self.closes = []
        self.order = None

    def next(self):
        self.closes.append(float(self.data.close[0]))
        self.order = self.buy(size=1)
        self.env.runstop()


class _ConfiguredObserver(bt.Strategy):
    params = (
        ("enabled", None),
        ("labels", None),
        ("nullable", "default"),
        ("options", None),
        ("threshold", None),
        ("window", None),
    )

    def __init__(self):
        self.received_parameters = {
            "enabled": self.p.enabled,
            "labels": self.p.labels,
            "nullable": self.p.nullable,
            "options": self.p.options,
            "threshold": self.p.threshold,
            "window": self.p.window,
        }
        self.closes = []

    def next(self):
        self.closes.append(float(self.data.close[0]))
        self.env.runstop()


def _resource_clients(environment, *, binding_kind="bar", wire_timeframe="1m"):
    # These labels are deliberately fixture-only and do not assert a real venue.
    account = AccountKey("fixture-provider", "spot", "fixture-" + environment, "fixture-account")
    resource = AccountResourceKey("spot", "fixture-wallet")
    instrument = MarketInstrumentKey(
        "fixture-provider",
        "spot",
        "fixture-" + environment,
        "spot",
        "BTC-USD",
        "BTCUSD",
    )
    if binding_kind == "tick":
        key = MarketSubscriptionKey(
            "fixture-source", "fixture-entitlement", instrument, "ctp.quote.v2"
        )
    elif binding_kind == "orderbook":
        key = MarketSubscriptionKey(
            "fixture-source", "fixture-entitlement", instrument, "orderbook", depth=5
        )
    else:
        key = MarketSubscriptionKey(
            "fixture-source",
            "fixture-entitlement",
            instrument,
            "bar",
            timeframe=wire_timeframe,
        )
    scope = WireScope("fixture-domain", account_ref="fixture-account")
    return account, resource, key, scope


def _factory(
    environment,
    created,
    *,
    binding_kind="bar",
    wire_timeframe="1m",
    **market_options,
):
    def create_proxy():
        account, resource, key, scope = _resource_clients(
            environment, binding_kind=binding_kind, wire_timeframe=wire_timeframe
        )
        account_client = _AccountFacade(account, resource)
        market_client = _MarketFacade(key, scope, **market_options)
        if binding_kind == "tick":
            binding = ServiceCtpTickBinding(
                market_client=market_client,
                resource=resource,
                qty_unit="contracts",
                account_session_generation="fixture-generation",
            )
        elif binding_kind == "orderbook":
            binding = ServiceOrderBookBinding(
                market_client=market_client,
                resource=resource,
                qty_unit="contracts",
                max_depth=5,
            )
        else:
            binding = ServiceFeedBinding(
                market_client=market_client,
                resource=resource,
                qty_unit="contracts",
                timeframe=wire_timeframe,
                history_start_utc="2026-10-01T12:00:00Z",
                history_end_utc="2026-10-01T12:02:00Z",
            )
        proxy = ServiceReadProxy(
            account_client,
            {"BTC-USD": binding},
            valuation_currency="USD",
            position_mode="net",
            clock=lambda: _NOW,
        )
        created.append((proxy, account_client, market_client))
        return proxy

    return create_proxy


def _route(environment):
    suffix = environment.value
    return TrustedRoute(
        environment,
        References(
            strategy="strategy/shared_observer",
            provider="provider/service_boundary",
            endpoint="endpoint/" + suffix,
            account="account/" + suffix,
            credential="credential/" + suffix,
            scope="scope/" + suffix,
        ),
    )


def _runtime_bindings(created, *, feed_spec=None, paper_factory=None, live_factory=None):
    paper = _route(Environment.PAPER)
    live = _route(Environment.LIVE)
    routes = TrustedRegistry((paper, live))
    feeds = (feed_spec or FeedSpec("BTC-USD"),)
    return RuntimeBindings(
        routes,
        (
            RouteBinding(
                paper,
                _ObserveAndAttemptBuy,
                feeds,
                paper_factory or _factory("paper", created),
            ),
            RouteBinding(
                live,
                _ObserveAndAttemptBuy,
                feeds,
                live_factory or _factory("live", created),
            ),
        ),
    ), {Environment.PAPER: paper, Environment.LIVE: live}


_V2_PARAMETER_CONTRACT = (
    ("enabled", "bool", True),
    ("labels", "list", True),
    ("nullable", "null", True),
    ("options", "map", True),
    ("threshold", "float", True),
    ("window", "int", True),
)


def _route_v2(environment, *, capabilities=("account.read", "market.read")):
    suffix = environment.value
    return TrustedRouteV2(
        environment,
        ReferencesV2(
            strategy="strategy/shared_configured_observer",
            provider="provider/service_boundary",
            endpoint="endpoint/" + suffix,
            account="account/" + suffix,
            credential="credential/" + suffix,
            scope="scope/" + suffix,
            product="product/binance_usdm",
            data_source="data_source/market_feed",
            clock="clock/exchange_utc",
        ),
        tuple(capabilities),
        _V2_PARAMETER_CONTRACT,
    )


def _runtime_bindings_v2(created, factory_calls=None, *, paper_capabilities=None):
    paper_capabilities = paper_capabilities or ("account.read", "market.read")
    paper = _route_v2(Environment.PAPER, capabilities=paper_capabilities)
    live = _route_v2(Environment.LIVE)
    contract_registry = TrustedRegistryV2((paper, live))
    feeds = (FeedSpec("BTC-USD"),)

    def counted_factory(environment):
        create = _factory(environment.value, created)

        def factory():
            if factory_calls is not None:
                factory_calls.append(environment)
            return create()

        return factory

    return RuntimeBindingsV2(
        contract_registry,
        (
            RouteBindingV2(paper, _ConfiguredObserver, feeds, counted_factory(Environment.PAPER)),
            RouteBindingV2(live, _ConfiguredObserver, feeds, counted_factory(Environment.LIVE)),
        ),
    ), {Environment.PAPER: paper, Environment.LIVE: live}


def _yaml_config(route, *, mode=None, environment=None, plugins="{}", extra=""):
    refs = route.references
    mode = mode or route.environment.value
    environment = environment or route.environment.value
    values = {
        name: getattr(refs, name)
        for name in ("strategy", "provider", "endpoint", "account", "credential", "scope")
    }
    lines = [
        "schema_version: 1",
        "mode: " + mode,
        "environment: " + environment,
        "references:",
    ]
    lines.extend(f"  {name}: {value}" for name, value in values.items())
    lines.append("plugins: " + plugins)
    if extra:
        lines.append(extra)
    return "\n".join(lines) + "\n"


def _yaml_v2_config(
    route,
    *,
    mode=None,
    environment=None,
    capabilities=("account.read", "market.read"),
    plugins="{gateway: false, risk: false, monitor: false, execution: false, runtime: false}",
    parameters=None,
    references_override=None,
):
    refs = route.references
    mode = mode or route.environment.value
    environment = environment or route.environment.value
    reference_values = {
        name: getattr(refs, name)
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
    if references_override:
        reference_values.update(references_override)
    parameter_lines = parameters if parameters is not None else [
        "  enabled: true",
        "  labels: [primary, null]",
        "  nullable: null",
        "  options:",
        "    nested:",
        "      count: 2",
        "      tags: [a, b]",
        "  threshold: 0.5",
        "  window: 20",
    ]
    lines = [
        "schema_version: 2",
        "mode: " + mode,
        "environment: " + environment,
        "references:",
    ]
    lines.extend(f"  {name}: {value}" for name, value in reference_values.items())
    lines.append("plugins: " + plugins)
    lines.append("strategy_parameters:")
    lines.extend(parameter_lines)
    lines.append("requested_capabilities:")
    lines.extend("  - " + capability for capability in capabilities)
    return "\n".join(lines) + "\n"


def test_same_registered_strategy_runs_through_real_engine_in_paper_and_live():
    created = []
    registry, routes = _runtime_bindings(created)
    results = []

    for environment in (Environment.PAPER, Environment.LIVE):
        session = assemble_run(_yaml_config(routes[environment]), registry)
        assert session.environment is environment
        strategies = session.run()
        (strategy,) = strategies
        results.append(strategy)
        assert type(strategy) is _ObserveAndAttemptBuy
        assert type(strategy.broker) is BtApiBroker
        assert type(strategy.broker.store) is BtApiStore
        assert type(strategy.datas[0]) is BtApiFeed
        assert strategy.closes == [100.5]
        assert strategy.order is not None
        assert strategy.order.info.get("error_code") == "market_data_only"

    assert type(results[0]) is type(results[1]) is _ObserveAndAttemptBuy
    assert len(created) == 2
    for proxy, account, market in created:
        assert type(proxy) is ServiceReadProxy
        assert proxy.is_connected is False
        assert account.refresh_calls == 1
        assert account.close_calls == 1
        assert market.history_calls == 1
        assert market.close_calls == 1
        assert account.write_calls == []
        assert market.write_calls == []


@pytest.mark.parametrize(
    "source, expected_code",
    [
        ("not: [valid", "invalid_yaml"),
        ("schema_version: 1\nmode: replay\n", "mode_deferred"),
    ],
)
def test_invalid_or_deferred_config_fails_before_provider_factory(source, expected_code):
    created = []
    registry, _routes = _runtime_bindings(created)
    with pytest.raises(RunConfigError) as caught:
        assemble_run(source, registry)
    assert caught.value.code == expected_code
    assert created == []


def test_valid_schema_v2_config_is_rejected_before_v1_provider_factory():
    source = """\
schema_version: 2
mode: paper
environment: paper
references:
  strategy: strategy/shared_observer
  provider: provider/service_boundary
  endpoint: endpoint/paper
  account: account/paper
  credential: credential/paper
  scope: scope/paper
  product: product/binance_usdm
  data_source: data_source/market_feed
  clock: clock/exchange_utc
plugins:
  gateway: false
  risk: false
  monitor: false
  execution: false
  runtime: false
strategy_parameters:
  window: 20
  threshold: 0.5
  use_filter: true
requested_capabilities:
  - market.read
  - account.read
"""
    assert type(parse_config(source)) is ValidatedRunConfigV2

    created = []
    factory_calls = []

    def counted_factory():
        factory_calls.append(True)
        return _factory("paper", created)()

    registry, _routes = _runtime_bindings(created, paper_factory=counted_factory)
    with pytest.raises(RuntimeAssemblyError) as caught:
        assemble_run(source, registry)

    assert caught.value.code == "invalid_runtime_registry"
    assert factory_calls == []
    assert created == []


def test_schema_v2_assembles_same_strategy_parameters_and_real_feed_in_paper_and_live():
    created = []
    factory_calls = []
    registry, routes = _runtime_bindings_v2(created, factory_calls)
    received = []

    for environment in (Environment.PAPER, Environment.LIVE):
        source = _yaml_v2_config(routes[environment])
        assert isinstance(parse_config(source), ValidatedRunConfigV2)
        session = assemble_run(source, registry)
        assert session.environment is environment
        (strategy,) = session.run()
        assert type(strategy) is _ConfiguredObserver
        assert type(strategy.broker) is BtApiBroker
        assert type(strategy.broker.store) is BtApiStore
        assert type(strategy.datas[0]) is BtApiFeed
        assert strategy.closes == [100.5]
        assert strategy.received_parameters == {
            "enabled": True,
            "labels": ["primary", None],
            "nullable": None,
            "options": {"nested": {"count": 2, "tags": ["a", "b"]}},
            "threshold": 0.5,
            "window": 20,
        }
        assert type(strategy.received_parameters["labels"]) is list
        assert type(strategy.received_parameters["options"]) is dict
        assert type(strategy.received_parameters["options"]["nested"]["tags"]) is list
        received.append(strategy.received_parameters)

    assert received[0] == received[1]
    assert factory_calls == [Environment.PAPER, Environment.LIVE]
    assert len(created) == 2
    for proxy, account, market in created:
        assert type(proxy) is ServiceReadProxy
        assert proxy.is_connected is False
        assert account.refresh_calls == 1
        assert account.close_calls == 1
        assert market.history_calls == 1
        assert market.close_calls == 1
        assert account.write_calls == []
        assert market.write_calls == []


@pytest.mark.parametrize(
    "options, expected_code",
    [
        ({"capabilities": ("market.read",)}, "required_read_capability_missing"),
        ({"capabilities": ("account.read",)}, "required_read_capability_missing"),
        (
            {
                "capabilities": (
                    "account.read",
                    "market.read",
                    "orders.paper.write",
                )
            },
            "capability_resolution_deferred",
        ),
        (
            {
                "plugins": "{gateway: false, risk: true, monitor: false, execution: false, runtime: false}"
            },
            "plugin_resolution_deferred",
        ),
        (
            {"references_override": {"product": "product/unknown"}},
            "route_mismatch",
        ),
        ({"mode": "live"}, "environment_mismatch"),
        (
            {
                "parameters": [
                    "  enabled: true",
                    "  labels: [primary, null]",
                    "  nullable: null",
                    "  options:",
                    "    nested:",
                    "      count: 2",
                    "      tags: [a, b]",
                    "  threshold: 0.5",
                    '  window: "20"',
                ]
            },
            "strategy_parameter_type_mismatch",
        ),
        (
            {
                "parameters": [
                    "  enabled: true",
                    "  extra: 1",
                    "  labels: [primary, null]",
                    "  nullable: null",
                    "  options:",
                    "    nested:",
                    "      count: 2",
                    "      tags: [a, b]",
                    "  threshold: 0.5",
                    "  window: 20",
                ]
            },
            "unknown_strategy_parameter",
        ),
        (
            {
                "parameters": [
                    "  enabled: true",
                    "  labels: [primary, null]",
                    "  nullable: null",
                    "  options:",
                    "    nested:",
                    "      count: 2",
                    "      tags: [a, b]",
                    "  threshold: 0.5",
                ]
            },
            "missing_strategy_parameter",
        ),
    ],
)
def test_schema_v2_rejects_capability_plugin_route_mode_and_parameter_mismatches_before_factory(
    options, expected_code
):
    created = []
    factory_calls = []
    registry, routes = _runtime_bindings_v2(created, factory_calls)
    source = _yaml_v2_config(routes[Environment.PAPER], **options)

    with pytest.raises((RunConfigError, RuntimeAssemblyError)) as caught:
        assemble_run(source, registry)

    assert caught.value.code == expected_code
    assert factory_calls == []
    assert created == []


def test_schema_v2_requires_the_code_route_to_declare_both_read_capabilities():
    created = []
    factory_calls = []
    registry, routes = _runtime_bindings_v2(
        created,
        factory_calls,
        paper_capabilities=("account.read",),
    )

    with pytest.raises(RunConfigError) as caught:
        assemble_run(_yaml_v2_config(routes[Environment.PAPER]), registry)

    assert caught.value.code == "capability_not_declared"
    assert factory_calls == []
    assert created == []


def test_schema_v2_same_strategy_reference_cannot_bind_different_classes_by_mode():
    created = []
    _registry, routes = _runtime_bindings_v2(created)

    class _OtherConfiguredObserver(_ConfiguredObserver):
        pass

    with pytest.raises(RuntimeAssemblyError) as caught:
        RuntimeBindingsV2(
            TrustedRegistryV2((routes[Environment.PAPER], routes[Environment.LIVE])),
            (
                RouteBindingV2(
                    routes[Environment.PAPER],
                    _ConfiguredObserver,
                    (FeedSpec("BTC-USD"),),
                    _factory("paper", created),
                ),
                RouteBindingV2(
                    routes[Environment.LIVE],
                    _OtherConfiguredObserver,
                    (FeedSpec("BTC-USD"),),
                    _factory("live", created),
                ),
            ),
        )

    assert caught.value.code == "strategy_binding_mismatch"
    assert created == []


@pytest.mark.parametrize(
    "mutate, expected_code",
    [
        (lambda text: text.replace("mode: paper", "mode: live"), "environment_mismatch"),
        (
            lambda text: text.replace("provider/service_boundary", "provider/unknown"),
            "unregistered_route",
        ),
        (
            lambda text: text.replace("plugins: {}", "plugins: {local_simulation: false}"),
            "plugins_unsupported",
        ),
        (
            lambda text: text.replace("plugins: {}", "plugins: {local_simulation: true}"),
            "plugin_resolution_deferred",
        ),
        (
            lambda text: text.replace("plugins: {}", "plugins: {}\nprovider_factory: os.system"),
            "unknown_field",
        ),
    ],
)
def test_mode_route_plugin_and_import_selection_fail_before_factory(mutate, expected_code):
    created = []
    registry, routes = _runtime_bindings(created)
    source = mutate(_yaml_config(routes[Environment.PAPER]))
    with pytest.raises((RunConfigError, RuntimeAssemblyError)) as caught:
        assemble_run(source, registry)
    assert caught.value.code == expected_code
    assert created == []


def test_runtime_revalidates_code_binding_before_provider_factory():
    created = []
    bad_feed = FeedSpec("BTC-USD")
    registry, routes = _runtime_bindings(created, feed_spec=bad_feed)
    object.__setattr__(bad_feed, "market_timeframe", "1h")

    with pytest.raises(RuntimeAssemblyError) as caught:
        assemble_run(_yaml_config(routes[Environment.PAPER]), registry)

    assert caught.value.code == "unsupported_feed_timeframe"
    assert created == []


def test_explicit_close_before_run_closes_each_injected_facade_once():
    created = []
    registry, routes = _runtime_bindings(created)
    session = assemble_run(_yaml_config(routes[Environment.PAPER]), registry)
    session.close()
    session.close()

    (proxy, account, market) = created[0]
    assert proxy.is_connected is False
    assert account.close_calls == 1
    assert market.close_calls == 1
    with pytest.raises(RuntimeAssemblyError, match="session_closed"):
        session.run()


def test_missing_backtrader_bar_history_capability_fails_before_factory(monkeypatch):
    created = []
    registry, routes = _runtime_bindings(created)
    monkeypatch.delattr(ServiceReadProxy, "supports_bar_history")

    with pytest.raises(RuntimeAssemblyError) as caught:
        assemble_run(_yaml_config(routes[Environment.PAPER]), registry)

    assert caught.value.code == "backtrader_bar_history_capability_unavailable"
    assert created == []


def test_provider_factory_result_must_be_the_real_read_only_proxy():
    paper = _route(Environment.PAPER)
    live = _route(Environment.LIVE)

    class _UnknownFactoryResult:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    unknown = _UnknownFactoryResult()
    registry = RuntimeBindings(
        TrustedRegistry((paper, live)),
        (
            RouteBinding(paper, _ObserveAndAttemptBuy, (FeedSpec("BTC-USD"),), lambda: unknown),
            RouteBinding(live, _ObserveAndAttemptBuy, (FeedSpec("BTC-USD"),), lambda: unknown),
        ),
    )

    with pytest.raises(RuntimeAssemblyError) as caught:
        assemble_run(_yaml_config(paper), registry)

    assert caught.value.code == "provider_factory_returned_unsupported_client"
    assert unknown.close_calls == 0


@pytest.mark.parametrize(
    "binding_kind, wire_timeframe",
    [("tick", "1m"), ("orderbook", "1m"), ("bar", "5m")],
)
def test_non_bar_or_non_minute_binding_fails_before_connect_or_subscribe(
    binding_kind, wire_timeframe
):
    created = []
    factory = _factory(
        "paper",
        created,
        binding_kind=binding_kind,
        wire_timeframe=wire_timeframe,
    )
    registry, routes = _runtime_bindings(created, paper_factory=factory)

    with pytest.raises(RuntimeAssemblyError) as caught:
        assemble_run(_yaml_config(routes[Environment.PAPER]), registry)

    assert caught.value.code == "provider_bar_history_binding_missing"
    proxy, account, market = created[0]
    assert account.refresh_calls == 0
    assert market.start_calls == 0
    assert market.history_calls == 0
    assert market.poll_calls == 0
    assert account.close_calls == 1
    assert market.close_calls == 1
    assert proxy.is_connected is False


def test_factory_failure_must_release_unhanded_facades():
    created = []

    def failure_atomic_factory():
        proxy = _factory("paper", created)()
        proxy.disconnect()
        raise RuntimeError("fixture factory failure after allocation")

    paper = _route(Environment.PAPER)
    live = _route(Environment.LIVE)
    registry = RuntimeBindings(
        TrustedRegistry((paper, live)),
        (
            RouteBinding(
                paper, _ObserveAndAttemptBuy, (FeedSpec("BTC-USD"),), failure_atomic_factory
            ),
            RouteBinding(
                live, _ObserveAndAttemptBuy, (FeedSpec("BTC-USD"),), _factory("live", created)
            ),
        ),
    )

    with pytest.raises(RuntimeAssemblyError) as caught:
        assemble_run(_yaml_config(paper), registry)

    assert caught.value.code == "provider_factory_failed"
    assert created[0][1].close_calls == 1
    assert created[0][2].close_calls == 1


def test_same_strategy_reference_cannot_select_different_strategy_classes():
    class OtherStrategy(bt.Strategy):
        pass

    created = []
    paper = _route(Environment.PAPER)
    live = _route(Environment.LIVE)
    with pytest.raises(RuntimeAssemblyError) as caught:
        RuntimeBindings(
            TrustedRegistry((paper, live)),
            (
                RouteBinding(
                    paper, _ObserveAndAttemptBuy, (FeedSpec("BTC-USD"),), _factory("paper", created)
                ),
                RouteBinding(
                    live, OtherStrategy, (FeedSpec("BTC-USD"),), _factory("live", created)
                ),
            ),
        )

    assert caught.value.code == "strategy_binding_mismatch"
    assert created == []


def test_broker_start_failure_closes_facades_and_resets_store_state_once():
    created = []

    def failing_factory():
        proxy = _factory("paper", created)()
        created[-1][1].fail_refresh = True
        return proxy

    paper = _route(Environment.PAPER)
    live = _route(Environment.LIVE)
    registry = RuntimeBindings(
        TrustedRegistry((paper, live)),
        (
            RouteBinding(paper, _ObserveAndAttemptBuy, (FeedSpec("BTC-USD"),), failing_factory),
            RouteBinding(
                live, _ObserveAndAttemptBuy, (FeedSpec("BTC-USD"),), _factory("live", created)
            ),
        ),
    )
    session = assemble_run(_yaml_config(paper), registry)

    with pytest.raises(RuntimeError, match="fixture account refresh failure"):
        session.run()

    _proxy, account, market = created[0]
    assert account.close_calls == 1
    assert market.close_calls == 1
    assert session._store.is_connected is False
    assert session._store._started is False
    assert session._broker._live_started is False


def test_feed_history_start_failure_compensates_real_engine_components():
    created = []
    registry, routes = _runtime_bindings(
        created,
        paper_factory=_factory("paper", created, fail_history=True),
    )
    session = assemble_run(_yaml_config(routes[Environment.PAPER]), registry)

    with pytest.raises(RuntimeError, match="fixture history failure"):
        session.run()

    proxy, account, market = created[0]
    (feed,) = session._cerebro.datas
    assert market.start_calls == 1
    assert market.history_calls == 1
    assert feed._session_active is False
    assert session._broker._live_started is False
    assert session._store.is_connected is False
    assert session._store._started is False
    assert proxy.is_connected is False
    assert account.close_calls == 1
    assert market.close_calls == 1
    assert account.write_calls == []
    assert market.write_calls == []


def test_request_stop_before_run_is_explicitly_not_accepted():
    created = []
    registry, routes = _runtime_bindings(created)
    session = assemble_run(_yaml_config(routes[Environment.PAPER]), registry)

    assert session.is_running is False
    assert session.request_stop() is False
    (strategy,) = session.run()

    assert strategy.closes == [100.5]
    assert created[0][1].close_calls == 1
    assert created[0][2].close_calls == 1


def test_idle_live_feed_stops_from_another_thread_and_owner_cleans_resources():
    created = []
    registry, routes = _runtime_bindings(
        created,
        paper_factory=_factory("paper", created, empty_history=True),
    )
    ready = threading.Event()
    begin_run = threading.Event()
    outcome = {}
    errors = []
    source = _yaml_config(routes[Environment.PAPER])

    def run_on_owner_thread():
        try:
            session = assemble_run(source, registry)
            outcome["session"] = session
            ready.set()
            if not begin_run.wait(timeout=5):
                raise RuntimeError("fixture run-start barrier timed out")
            outcome["strategies"] = session.run()
        except Exception as exc:  # noqa: BLE001 - pass worker failures back to the test
            errors.append(exc)
            ready.set()

    owner = threading.Thread(target=run_on_owner_thread)
    owner.start()
    session = None
    try:
        assert ready.wait(timeout=5)
        session = outcome["session"]
        proxy, account, market = created[0]
        # A stop racing just before Cerebro publishes its active scope is
        # rejected, not accepted and silently cleared by _begin_run.
        assert session.request_stop() is False
        begin_run.set()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (not session.is_running or not market.poll_calls):
            time.sleep(0.01)
        assert session.is_running is True
        assert market.history_calls == 1
        assert market.poll_calls > 0
        assert session.request_stop() is True
    finally:
        begin_run.set()
        if session is not None:
            deadline = time.monotonic() + 5
            while owner.is_alive() and not session.is_running and time.monotonic() < deadline:
                time.sleep(0.01)
            if session.is_running:
                session.request_stop()
        owner.join(timeout=5)

    assert not owner.is_alive()
    assert errors == []
    (strategy,) = outcome["strategies"]
    assert strategy.closes == []
    assert proxy.is_connected is False
    assert session._store.is_connected is False
    assert session._store._started is False
    assert session._broker._live_started is False
    assert account.close_calls == 1
    assert market.close_calls == 1
    assert account.write_calls == []
    assert market.write_calls == []


def test_run_keeps_proxy_when_store_reports_metadata_probe_shutdown_incomplete():
    created = []
    registry, routes = _runtime_bindings(
        created,
        paper_factory=_factory("paper", created, empty_history=True),
    )
    session = assemble_run(_yaml_config(routes[Environment.PAPER]), registry)
    proxy, account, market = created[0]
    release_probe = threading.Event()
    probe_started = threading.Event()
    poll_seen = threading.Event()
    probe_worker = {}
    stop_answers = []

    def fake_probe_worker():
        probe_started.set()
        release_probe.wait(timeout=5)

    def mark_probe_active():
        if probe_started.is_set():
            return
        worker = threading.Thread(target=fake_probe_worker)
        probe_worker["thread"] = worker
        worker.start()
        with session._store._read_only_metadata_probe_condition:
            session._store._read_only_metadata_probe_thread = worker
            session._store._read_only_metadata_probe_active = True
        poll_seen.set()

    market.on_poll = mark_probe_active

    def request_stop_after_poll():
        if poll_seen.wait(timeout=5):
            stop_answers.append(session.request_stop())

    requester = threading.Thread(target=request_stop_after_poll)
    requester.start()
    try:
        with pytest.raises(RuntimeAssemblyError) as caught:
            session.run()
        assert caught.value.code == "provider_cleanup_incomplete"
        requester.join(timeout=5)
        assert not requester.is_alive()
        assert stop_answers == [True]
        assert probe_worker["thread"].is_alive()
        assert session._store._service_read_only_stopped is False
        assert proxy.is_connected is True
        assert account.close_calls == 0
        assert market.close_calls == 0
    finally:
        if session.is_running:
            session.request_stop()
        release_probe.set()
        worker = probe_worker.get("thread")
        if worker is not None:
            worker.join(timeout=5)
        with session._store._read_only_metadata_probe_condition:
            session._store._read_only_metadata_probe_active = False
            session._store._read_only_metadata_probe_thread = None
            session._store._read_only_metadata_probe_condition.notify_all()
        requester.join(timeout=5)
        if not session._closed:
            session.close()

    assert not requester.is_alive()
    assert worker is not None and not worker.is_alive()
    assert session._store._service_read_only_stopped is True
    assert proxy.is_connected is False
    assert account.close_calls == 1
    assert market.close_calls == 1


def test_run_and_close_reject_wrong_owner_thread_without_touching_proxy():
    created = []
    registry, routes = _runtime_bindings(created)
    session = assemble_run(_yaml_config(routes[Environment.PAPER]), registry)
    rejected = []

    def wrong_thread_calls():
        for operation in (session.run, session.close):
            try:
                operation()
            except RuntimeAssemblyError as exc:
                rejected.append(exc.code)

    worker = threading.Thread(target=wrong_thread_calls)
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert rejected == ["session_wrong_owner_thread", "session_wrong_owner_thread"]
    assert created[0][1].close_calls == 0
    assert created[0][2].close_calls == 0
    session.close()
    assert created[0][1].close_calls == 1
    assert created[0][2].close_calls == 1


def test_no_feed_defensive_guard_is_not_a_zero_history_live_test():
    # This private-state corruption check only exercises the defensive guard;
    # the real empty-history/live-poll path is covered above.
    created = []
    registry, routes = _runtime_bindings(created)
    session = assemble_run(_yaml_config(routes[Environment.PAPER]), registry)
    session._cerebro.datas.clear()

    with pytest.raises(RuntimeAssemblyError) as caught:
        session.run()

    assert caught.value.code == "session_has_no_feeds"
    _proxy, account, market = created[0]
    assert account.close_calls == 1
    assert market.close_calls == 1


def test_store_readiness_failure_after_proxy_connect_is_stopped_through_store():
    created = []

    def factory_with_late_snapshot_failure():
        proxy = _factory("paper", created)()
        created[-1][1].fail_snapshot_then_refresh = True
        return proxy

    registry, routes = _runtime_bindings(created, paper_factory=factory_with_late_snapshot_failure)
    session = assemble_run(_yaml_config(routes[Environment.PAPER]), registry)

    with pytest.raises(RuntimeError, match="fixture account refresh failure"):
        session.run()

    proxy, account, market = created[0]
    assert proxy.is_connected is False
    assert session._store._connected is False
    assert session._store._started is False
    assert session._store._service_read_only_stopped is True
    assert account.refresh_calls == 2
    assert account.snapshot_calls == 1
    assert account.close_calls == 1
    assert market.close_calls == 1
