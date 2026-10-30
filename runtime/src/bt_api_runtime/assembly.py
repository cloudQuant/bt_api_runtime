"""Build one ordinary Backtrader paper/live read-only session.

This module does not discover code from YAML, construct an SDK runtime, prove
provider identity, or grant permission. Application code owns the binding
registry and the provider factory. The factory must return a fresh
``ServiceReadProxy`` made from the application's already-bound read-only
account and market facades.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from backtrader.brokers.btapibroker import BtApiBroker
from backtrader.cerebro import Cerebro
from backtrader.dataseries import TimeFrame
from backtrader.feeds.btapifeed import BtApiFeed
from backtrader.stores.btapistore import BtApiStore
from backtrader.stores.service_read_proxy import ServiceReadProxy
from backtrader.strategy import Strategy
from bt_api_run_contract import (
    Environment,
    References,
    ReferencesV2,
    ResolvedPlan,
    ResolvedPlanV2,
    RunConfigError,
    TrustedRegistry,
    TrustedRegistryV2,
    TrustedRoute,
    TrustedRouteV2,
    ValidatedRunConfig,
    ValidatedRunConfigV2,
    parse_config,
    resolve_plan,
    validate_plan,
)

_REFERENCE_FIELDS = ("strategy", "provider", "endpoint", "account", "credential", "scope")
_REFERENCE_FIELDS_V2 = (
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
_REQUIRED_V2_READ_CAPABILITIES = frozenset(("market.read", "account.read"))
_SUPPORTED_MARKET_TIMEFRAME = "1m"


class RuntimeAssemblyError(RuntimeError):
    """Finite, non-sensitive failure from runtime binding or assembly."""

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code.isascii() or not code:
            raise ValueError("code must be a non-empty ASCII string")
        self.code = code
        super().__init__(code)


_CLEANUP_STEP_CODES = frozenset(
    (
        "broker_stop",
        "component_inventory",
        "component_state",
        "component_stop",
        "store_state",
        "store_stop",
        "store_stop_unconfirmed",
        "metadata_probe_state",
        "metadata_probe_shutdown",
        "proxy_disconnect",
        "proxy_state",
        "cleanup_state_unknown",
    )
)


@dataclass(frozen=True)
class RuntimeCleanupReport:
    """Finite cleanup observations without provider exception details."""

    failed_steps: tuple[str, ...] = ()
    pending_steps: tuple[str, ...] = ()
    confirmed_steps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for values in (self.failed_steps, self.pending_steps, self.confirmed_steps):
            if type(values) is not tuple or any(
                type(value) is not str or value not in _CLEANUP_STEP_CODES for value in values
            ):
                raise ValueError("cleanup report contains an unsupported step code")

    @property
    def complete(self) -> bool:
        """Whether every required cleanup action returned and state was confirmed."""
        return not self.failed_steps and not self.pending_steps


@dataclass(frozen=True)
class _CleanupOutcome:
    report: RuntimeCleanupReport
    interruption: BaseException | None = None


def _cleanup_report(
    *,
    failed: list[str] | tuple[str, ...] = (),
    pending: list[str] | tuple[str, ...] = (),
    confirmed: list[str] | tuple[str, ...] = (),
) -> RuntimeCleanupReport:
    def unique(values):
        return tuple(dict.fromkeys(values))

    return RuntimeCleanupReport(unique(failed), unique(pending), unique(confirmed))


def _attach_cleanup_report(
    primary: BaseException,
    report: RuntimeCleanupReport,
    *,
    replace: RuntimeCleanupReport | None = None,
) -> bool:
    """Best-effort attach a safe report without replacing the primary error.

    Some exception objects have no writable instance dictionary. In that case
    a caller outside a partially completed assembly cannot observe this report;
    preserving the original exception takes precedence over wrapping it. A
    prior report is replaced only when the caller supplies that exact report
    as the expected value; unrelated attributes are preserved.
    """
    try:
        namespace = BaseException.__getattribute__(primary, "__dict__")
        if type(namespace) is not dict:
            return False
        if "runtime_cleanup_report" in namespace:
            existing = namespace["runtime_cleanup_report"]
            if existing is report:
                return True
            if replace is None or existing is not replace:
                return False
        dict.__setitem__(namespace, "runtime_cleanup_report", report)
    except BaseException:  # noqa: BLE001 - diagnostics must never mask primary failure
        return False
    return True


def _is_control_interrupt(error: BaseException) -> bool:
    return isinstance(error, (GeneratorExit, KeyboardInterrupt, SystemExit))


def _disconnect_proxy(proxy: ServiceReadProxy) -> _CleanupOutcome:
    failed = []
    pending = []
    confirmed = []
    interruption = None
    try:
        proxy.disconnect()
    except BaseException as error:  # noqa: BLE001 - retain only finite cleanup state
        failed.append("proxy_disconnect")
        # ServiceReadProxy.is_connected is false while a failed disconnect is
        # still in its closing state. A raised close therefore stays pending.
        pending.append("proxy_disconnect")
        if _is_control_interrupt(error):
            interruption = error
    else:
        confirmed.append("proxy_disconnect")

    return _CleanupOutcome(
        _cleanup_report(failed=failed, pending=pending, confirmed=confirmed), interruption
    )


@dataclass(frozen=True)
class FeedSpec:
    """One code-bound minute-bar feed declaration."""

    dataname: str
    market_timeframe: str = _SUPPORTED_MARKET_TIMEFRAME

    def __post_init__(self) -> None:
        if type(self.dataname) is not str or not self.dataname or "\x00" in self.dataname:
            raise RuntimeAssemblyError("invalid_feed_dataname")
        if type(self.market_timeframe) is not str or self.market_timeframe != "1m":
            raise RuntimeAssemblyError("unsupported_feed_timeframe")


@dataclass(frozen=True)
class RouteBinding:
    """Code-owned strategy, feed, and read-only provider factory for one route."""

    route: TrustedRoute
    strategy_class: type
    feeds: tuple[FeedSpec, ...]
    provider_factory: Callable[[], ServiceReadProxy]

    def __post_init__(self) -> None:
        _validate_route_binding(self)


@dataclass(frozen=True)
class RouteBindingV2:
    """Code-owned strategy, feed, and provider factory for one v2 route."""

    route: TrustedRouteV2
    strategy_class: type
    feeds: tuple[FeedSpec, ...]
    provider_factory: Callable[[], ServiceReadProxy]

    def __post_init__(self) -> None:
        _validate_route_binding_v2(self)


class RuntimeBindings:
    """Pair every contract route with exactly one code-owned runtime binding."""

    __slots__ = ("_bindings", "_contract_registry")

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("RuntimeBindings cannot be subclassed")

    def __init__(self, contract_registry: TrustedRegistry, bindings: tuple[RouteBinding, ...]):
        object.__setattr__(self, "_contract_registry", contract_registry)
        object.__setattr__(self, "_bindings", bindings)
        _runtime_registry_parts(self)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("RuntimeBindings is immutable")

    @property
    def contract_registry(self) -> TrustedRegistry:
        return object.__getattribute__(self, "_contract_registry")

    @property
    def bindings(self) -> tuple[RouteBinding, ...]:
        return object.__getattribute__(self, "_bindings")

    def __repr__(self) -> str:
        return f"RuntimeBindings(route_count={len(self.bindings)!r})"


class RuntimeBindingsV2:
    """Pair every v2 contract route with one complete code-owned binding."""

    __slots__ = ("_bindings", "_contract_registry")

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("RuntimeBindingsV2 cannot be subclassed")

    def __init__(
        self,
        contract_registry: TrustedRegistryV2,
        bindings: tuple[RouteBindingV2, ...],
    ) -> None:
        object.__setattr__(self, "_contract_registry", contract_registry)
        object.__setattr__(self, "_bindings", bindings)
        _runtime_registry_v2_parts(self)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("RuntimeBindingsV2 is immutable")

    @property
    def contract_registry(self) -> TrustedRegistryV2:
        return object.__getattribute__(self, "_contract_registry")

    @property
    def bindings(self) -> tuple[RouteBindingV2, ...]:
        return object.__getattribute__(self, "_bindings")

    def __repr__(self) -> str:
        return f"RuntimeBindingsV2(route_count={len(self.bindings)!r})"


def _references_tuple(references: References) -> tuple[str, ...]:
    if type(references) is not References:
        raise RuntimeAssemblyError("invalid_route_references")
    try:
        values = tuple(object.__getattribute__(references, name) for name in _REFERENCE_FIELDS)
        References(*values)
    except (AttributeError, IndexError, TypeError, RunConfigError):
        raise RuntimeAssemblyError("invalid_route_references") from None
    return values


def _references_tuple_v2(references: ReferencesV2) -> tuple[str, ...]:
    if type(references) is not ReferencesV2:
        raise RuntimeAssemblyError("invalid_route_references")
    try:
        values = tuple(object.__getattribute__(references, name) for name in _REFERENCE_FIELDS_V2)
        ReferencesV2(*values)
    except (AttributeError, IndexError, TypeError, RunConfigError):
        raise RuntimeAssemblyError("invalid_route_references") from None
    return values


def _route_key(route: TrustedRoute) -> tuple[Environment, tuple[str, ...]]:
    if type(route) is not TrustedRoute:
        raise RuntimeAssemblyError("invalid_route_binding")
    try:
        environment = route.environment
        references = route.references
    except (AttributeError, RunConfigError):
        raise RuntimeAssemblyError("invalid_route_binding") from None
    if type(environment) is not Environment:
        raise RuntimeAssemblyError("invalid_route_binding")
    return environment, _references_tuple(references)


def _route_key_v2(
    route: TrustedRouteV2,
) -> tuple[Environment, tuple[str, ...], tuple[str, ...], tuple[tuple[str, str, bool], ...]]:
    if type(route) is not TrustedRouteV2:
        raise RuntimeAssemblyError("invalid_route_binding")
    try:
        environment = route.environment
        references = route.references
        capabilities = route.available_capabilities
        parameter_contract = route.strategy_parameter_contract
    except (AttributeError, RunConfigError):
        raise RuntimeAssemblyError("invalid_route_binding") from None
    if type(environment) is not Environment:
        raise RuntimeAssemblyError("invalid_route_binding")
    if type(capabilities) is not tuple or type(parameter_contract) is not tuple:
        raise RuntimeAssemblyError("invalid_route_binding")
    return environment, _references_tuple_v2(references), capabilities, parameter_contract


def _validate_feed_specs(feeds: object) -> tuple[FeedSpec, ...]:
    if type(feeds) is not tuple or not feeds:
        raise RuntimeAssemblyError("invalid_feed_binding")
    names = set()
    for feed in feeds:
        if type(feed) is not FeedSpec:
            raise RuntimeAssemblyError("invalid_feed_binding")
        FeedSpec.__post_init__(feed)
        if feed.dataname in names:
            raise RuntimeAssemblyError("duplicate_feed_binding")
        names.add(feed.dataname)
    return feeds


def _validate_route_binding(binding: object) -> RouteBinding:
    if type(binding) is not RouteBinding:
        raise RuntimeAssemblyError("invalid_route_binding")
    try:
        route = object.__getattribute__(binding, "route")
        strategy_class = object.__getattribute__(binding, "strategy_class")
        feeds = object.__getattribute__(binding, "feeds")
        provider_factory = object.__getattribute__(binding, "provider_factory")
    except AttributeError:
        raise RuntimeAssemblyError("invalid_route_binding") from None
    _route_key(route)
    if type(strategy_class) is not type:
        raise RuntimeAssemblyError("invalid_strategy_binding")
    try:
        is_strategy = issubclass(strategy_class, Strategy)
    except TypeError:
        is_strategy = False
    if not is_strategy:
        raise RuntimeAssemblyError("invalid_strategy_binding")
    _validate_feed_specs(feeds)
    if not callable(provider_factory):
        raise RuntimeAssemblyError("invalid_provider_factory")
    return binding


def _validate_route_binding_v2(binding: object) -> RouteBindingV2:
    if type(binding) is not RouteBindingV2:
        raise RuntimeAssemblyError("invalid_route_binding")
    try:
        route = object.__getattribute__(binding, "route")
        strategy_class = object.__getattribute__(binding, "strategy_class")
        feeds = object.__getattribute__(binding, "feeds")
        provider_factory = object.__getattribute__(binding, "provider_factory")
    except AttributeError:
        raise RuntimeAssemblyError("invalid_route_binding") from None
    _route_key_v2(route)
    if type(strategy_class) is not type:
        raise RuntimeAssemblyError("invalid_strategy_binding")
    try:
        is_strategy = issubclass(strategy_class, Strategy)
    except TypeError:
        is_strategy = False
    if not is_strategy:
        raise RuntimeAssemblyError("invalid_strategy_binding")
    _validate_feed_specs(feeds)
    if not callable(provider_factory):
        raise RuntimeAssemblyError("invalid_provider_factory")
    return binding


def _runtime_registry_parts(
    registry: object,
) -> tuple[TrustedRegistry, tuple[RouteBinding, ...]]:
    if type(registry) is not RuntimeBindings:
        raise RuntimeAssemblyError("invalid_runtime_registry")
    try:
        contract_registry = object.__getattribute__(registry, "_contract_registry")
        bindings = object.__getattribute__(registry, "_bindings")
    except AttributeError:
        raise RuntimeAssemblyError("invalid_runtime_registry") from None
    if type(contract_registry) is not TrustedRegistry:
        raise RuntimeAssemblyError("invalid_runtime_registry")
    try:
        contract_routes = contract_registry.routes
    except (AttributeError, RunConfigError, TypeError):
        raise RuntimeAssemblyError("invalid_runtime_registry") from None
    if type(bindings) is not tuple or len(bindings) != len(contract_routes):
        raise RuntimeAssemblyError("route_binding_mismatch")
    strategy_classes = {}
    for binding in bindings:
        _validate_route_binding(binding)
        strategy_reference = binding.route.references.strategy
        previous_class = strategy_classes.setdefault(strategy_reference, binding.strategy_class)
        if previous_class is not binding.strategy_class:
            raise RuntimeAssemblyError("strategy_binding_mismatch")
    try:
        runtime_routes = tuple(binding.route for binding in bindings)
        checked_routes = TrustedRegistry(runtime_routes)
        if checked_routes.routes != contract_routes:
            raise RuntimeAssemblyError("route_binding_mismatch")
    except (AttributeError, RunConfigError, TypeError):
        raise RuntimeAssemblyError("route_binding_mismatch") from None
    return contract_registry, bindings


def _runtime_registry_v2_parts(
    registry: object,
) -> tuple[TrustedRegistryV2, tuple[RouteBindingV2, ...]]:
    if type(registry) is not RuntimeBindingsV2:
        raise RuntimeAssemblyError("invalid_runtime_registry")
    try:
        contract_registry = object.__getattribute__(registry, "_contract_registry")
        bindings = object.__getattribute__(registry, "_bindings")
    except AttributeError:
        raise RuntimeAssemblyError("invalid_runtime_registry") from None
    if type(contract_registry) is not TrustedRegistryV2:
        raise RuntimeAssemblyError("invalid_runtime_registry")
    try:
        contract_routes = contract_registry.routes
    except (AttributeError, RunConfigError, TypeError):
        raise RuntimeAssemblyError("invalid_runtime_registry") from None
    if type(bindings) is not tuple or len(bindings) != len(contract_routes):
        raise RuntimeAssemblyError("route_binding_mismatch")
    strategy_classes = {}
    runtime_routes = []
    for binding in bindings:
        _validate_route_binding_v2(binding)
        route = object.__getattribute__(binding, "route")
        key = _route_key_v2(route)
        strategy_reference = key[1][0]
        previous_class = strategy_classes.setdefault(
            strategy_reference, object.__getattribute__(binding, "strategy_class")
        )
        if previous_class is not object.__getattribute__(binding, "strategy_class"):
            raise RuntimeAssemblyError("strategy_binding_mismatch")
        runtime_routes.append(route)
    try:
        checked_routes = TrustedRegistryV2(tuple(runtime_routes))
        if checked_routes.routes != contract_routes:
            raise RuntimeAssemblyError("route_binding_mismatch")
    except RuntimeAssemblyError:
        raise
    except (AttributeError, RunConfigError, TypeError):
        raise RuntimeAssemblyError("route_binding_mismatch") from None
    return contract_registry, bindings


def _select_binding(
    plan: ResolvedPlan,
    bindings: tuple[RouteBinding, ...],
) -> RouteBinding:
    key = (plan.environment, _references_tuple(plan.references))
    matches = tuple(binding for binding in bindings if _route_key(binding.route) == key)
    if not matches:
        raise RuntimeAssemblyError("runtime_binding_missing")
    if len(matches) != 1:
        raise RuntimeAssemblyError("runtime_binding_ambiguous")
    return matches[0]


def _select_binding_v2(
    plan: ResolvedPlanV2,
    bindings: tuple[RouteBindingV2, ...],
) -> RouteBindingV2:
    key = (plan.environment, _references_tuple_v2(plan.references))
    matches = tuple(
        binding
        for binding in bindings
        if _route_key_v2(binding.route)[:2] == key
    )
    if not matches:
        raise RuntimeAssemblyError("runtime_binding_missing")
    if len(matches) != 1:
        raise RuntimeAssemblyError("runtime_binding_ambiguous")
    return matches[0]


class RunSession:
    """Single-use assembled run with an owner-thread proxy lifecycle.

    Cerebro owns normal component start/stop. The session drives the per-run
    ``ServiceReadProxy`` close boundary and never closes an SDK runtime. With a
    :class:`SdkReadOnlyFacadeFactory`, the factory's resource owner retains the
    proxy, account/market facades, and SDK runtime; after the session returns,
    the application calls that owner's ``close()`` to finish runtime cleanup.
    If startup fails before Cerebro enters its cleanup block, this session
    best-effort stops the actual Broker, Feed/Data, Store, and proxy on the
    construction/run owner thread. It does not wrap Strategy lifecycle hooks,
    so Strategy constructors and ``start()`` must not leave external resources
    behind when they raise.
    """

    __slots__ = (
        "_broker",
        "_cerebro",
        "_cleanup_attempted",
        "_cleanup_report",
        "_closed",
        "_environment",
        "_mode",
        "_owner_thread_id",
        "_proxy",
        "_ran",
        "_store",
    )

    def __init__(
        self,
        cerebro: Cerebro,
        store: BtApiStore,
        broker: BtApiBroker,
        proxy: ServiceReadProxy,
        config: ValidatedRunConfig | ValidatedRunConfigV2,
    ) -> None:
        self._cerebro = cerebro
        self._store = store
        self._broker = broker
        self._proxy = proxy
        self._mode = config.mode
        self._environment = config.environment
        self._owner_thread_id = threading.get_ident()
        self._ran = False
        self._closed = False
        self._cleanup_attempted = False
        self._cleanup_report: RuntimeCleanupReport | None = None

    def _assert_owner_thread(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeAssemblyError("session_wrong_owner_thread")

    def _mark_closed(self) -> None:
        with self._cerebro._runstop_lock:
            self._closed = True

    @property
    def mode(self):
        return self._mode

    @property
    def environment(self):
        return self._environment

    @property
    def cleanup_report(self) -> RuntimeCleanupReport | None:
        """Latest finite cleanup report, if this session attempted cleanup."""
        return self._cleanup_report

    @property
    def is_running(self) -> bool:
        """Return Cerebro's lock-protected active-run state from any thread."""
        with self._cerebro._runstop_lock:
            return bool(self._cerebro._run_active)

    def request_stop(self) -> bool:
        """Request ``Cerebro.runstop`` without closing any owner-thread resource.

        This method is safe to call from another thread. It returns whether an
        active run was observed under Cerebro's run-stop lock. Cleanup remains
        the responsibility of the thread executing :meth:`run`.
        """
        with self._cerebro._runstop_lock:
            if self._closed or not self._cerebro._run_active:
                return False
            # Cerebro uses an RLock, so this preserves its own synchronized
            # stop transition without duplicating its implementation.
            self._cerebro.runstop()
            return True

    @staticmethod
    def _unique_components(*collections):
        seen = set()
        for collection in collections:
            for component in collection:
                identity = id(component)
                if identity in seen:
                    continue
                seen.add(identity)
                yield component

    def _metadata_probe_shutdown_pending(self) -> bool:
        condition = self._store._read_only_metadata_probe_condition
        with condition:
            worker = self._store._read_only_metadata_probe_thread
            return bool(
                self._store._read_only_metadata_probe_active
                and worker is not None
                and worker is not threading.current_thread()
            )

    def _compensate_failed_run(self) -> _CleanupOutcome:
        """Best-effort unwind for exceptions before/inside engine cleanup.

        Each teardown layer is attempted even when an earlier layer fails.
        A successful Broker shutdown may stop the Store itself; Store state is
        checked before retrying it. The final direct proxy close is used only
        when no Store stop completed, including partial Store startup where the
        proxy connected before Store flags were committed. Exception details
        are intentionally discarded; only fixed cleanup step codes are kept.
        """
        failed = []
        pending = []
        confirmed = []
        interruption = None

        def capture_interrupt(error: BaseException) -> None:
            nonlocal interruption
            if interruption is None and _is_control_interrupt(error):
                interruption = error

        store_stop_confirmed = False
        store_state_known = True

        previous_report = self._cleanup_report
        if (
            previous_report is not None
            and "broker_stop" in previous_report.confirmed_steps
        ):
            # A prior owner-thread attempt completed the Broker's read-only
            # shutdown method. Retry an unconfirmed Store stop below without
            # rerunning its snapshot/summary path or asking it to stop Store
            # again. A Broker stop that raised remains retryable.
            confirmed.append("broker_stop")
        else:
            try:
                self._broker.stop()
            except BaseException as error:  # noqa: BLE001 - continue every cleanup layer
                failed.append("broker_stop")
                capture_interrupt(error)
            else:
                confirmed.append("broker_stop")

        try:
            store_stop_confirmed = self._store._service_read_only_stopped is True
        except BaseException as error:  # noqa: BLE001 - do not guess Store ownership state
            failed.append("store_state")
            pending.append("store_state")
            store_state_known = False
            capture_interrupt(error)
        else:
            if store_stop_confirmed:
                confirmed.append("store_stop")

        try:
            components = tuple(self._unique_components(self._cerebro.datas, self._cerebro.feeds))
        except BaseException as error:  # noqa: BLE001 - continue Store/proxy cleanup
            failed.append("component_inventory")
            pending.append("component_inventory")
            components = ()
            capture_interrupt(error)
        for component in components:
            # Cerebro may already have completed its normal main-loop cleanup
            # before surfacing a later exception. BtApiFeed publishes this
            # lifecycle state; never stop an already-stopped feed a second time.
            try:
                component_active = bool(
                    getattr(component, "_session_active", False)
                    or getattr(component, "_tick_consumer_claimed", False)
                )
            except BaseException as error:  # noqa: BLE001 - avoid an unproven duplicate stop
                failed.append("component_state")
                pending.append("component_state")
                capture_interrupt(error)
                continue
            if not component_active:
                continue
            try:
                component.stop()
            except BaseException as error:  # noqa: BLE001 - continue every cleanup layer
                failed.append("component_stop")
                pending.append("component_stop")
                capture_interrupt(error)
            else:
                confirmed.append("component_stop")

        # A read-only connect may succeed before Store.start commits _started;
        # a later account read can reset _connected while leaving the proxy
        # connected. Stop once unless the actual Store already confirmed it.
        if store_state_known and not store_stop_confirmed:
            try:
                self._store.stop()
            except BaseException as error:  # noqa: BLE001 - proxy close may still be attempted
                failed.append("store_stop")
                capture_interrupt(error)
            try:
                store_stop_confirmed = self._store._service_read_only_stopped is True
            except BaseException as error:  # noqa: BLE001 - do not guess whether proxy is owned
                failed.append("store_state")
                pending.append("store_state")
                store_state_known = False
                capture_interrupt(error)
            else:
                if store_stop_confirmed:
                    confirmed.append("store_stop")
                else:
                    pending.append("store_stop_unconfirmed")

        if not store_state_known:
            # The Store is the owner of the proxy boundary. If its stopped
            # state cannot be read, bypassing it with a direct disconnect could
            # race a Store-owned worker or repeat a close already in progress.
            pending.append("proxy_disconnect")
        elif not store_stop_confirmed:
            try:
                metadata_probe_pending = self._metadata_probe_shutdown_pending()
            except BaseException as error:  # noqa: BLE001
                failed.append("metadata_probe_state")
                pending.append("metadata_probe_shutdown")
                capture_interrupt(error)
            else:
                if metadata_probe_pending:
                    # BtApiStore refuses a concurrent disconnect while its metadata
                    # worker may still be reading this proxy. Preserve that custody.
                    pending.append("metadata_probe_shutdown")
                else:
                    outcome = _disconnect_proxy(self._proxy)
                    failed.extend(outcome.report.failed_steps)
                    pending.extend(outcome.report.pending_steps)
                    confirmed.extend(outcome.report.confirmed_steps)
                    if interruption is None:
                        interruption = outcome.interruption

        report = _cleanup_report(failed=failed, pending=pending, confirmed=confirmed)
        return _CleanupOutcome(report, interruption)

    def _record_cleanup_outcome(self, outcome: _CleanupOutcome) -> None:
        self._cleanup_report = outcome.report
        self._cleanup_attempted = True

    def _safe_compensate_failed_run(self) -> _CleanupOutcome:
        """Reduce unexpected compensation failures to finite, unconfirmed state."""
        try:
            return self._compensate_failed_run()
        except BaseException as error:  # noqa: BLE001 - never leak secondary cleanup details
            report = _cleanup_report(
                failed=("cleanup_state_unknown",), pending=("cleanup_state_unknown",)
            )
            interruption = error if _is_control_interrupt(error) else None
            return _CleanupOutcome(report, interruption)

    def _mark_closed_after_cleanup(
        self,
        outcome: _CleanupOutcome,
        *,
        primary: BaseException | None = None,
        error_code: str = "run_cleanup_failed",
    ) -> None:
        """Mark closure without allowing an unexpected failure to mask primary state."""
        try:
            self._mark_closed()
        except BaseException as error:
            report = _cleanup_report(
                failed=outcome.report.failed_steps + ("cleanup_state_unknown",),
                pending=outcome.report.pending_steps + ("cleanup_state_unknown",),
                confirmed=outcome.report.confirmed_steps,
            )
            self._record_cleanup_outcome(_CleanupOutcome(report))
            if primary is not None:
                _attach_cleanup_report(primary, report, replace=outcome.report)
                return
            _attach_cleanup_report(error, report)
            if _is_control_interrupt(error):
                raise
            sanitized = RuntimeAssemblyError(error_code)
            _attach_cleanup_report(sanitized, report)
            raise sanitized from None

    def _cleanup_preserving_primary(self, primary: BaseException) -> None:
        """Attempt owner-thread cleanup without replacing an active failure."""
        try:
            outcome = (
                self._safe_compensate_failed_run() if self._ran else _disconnect_proxy(self._proxy)
            )
        except BaseException:  # noqa: BLE001 - fallback must not mask the primary failure
            outcome = _CleanupOutcome(
                _cleanup_report(
                    failed=("cleanup_state_unknown",), pending=("cleanup_state_unknown",)
                )
            )
        self._record_cleanup_outcome(outcome)
        _attach_cleanup_report(primary, outcome.report)
        if outcome.report.complete:
            self._mark_closed_after_cleanup(outcome, primary=primary)

    def run(self) -> list[Strategy]:
        self._assert_owner_thread()
        if self._closed:
            raise RuntimeAssemblyError("session_closed")
        if self._ran:
            raise RuntimeAssemblyError("session_already_run")
        if not self._cerebro.datas:
            primary = RuntimeAssemblyError("session_has_no_feeds")
            self._cleanup_preserving_primary(primary)
            raise primary
        self._ran = True
        try:
            strategies = self._cerebro.run()
        except BaseException as primary:
            self._cleanup_preserving_primary(primary)
            raise

        # Cerebro normally stopped the Store in its own run lifecycle. It
        # ignores the Store's shutdown report, so do not bypass an unresolved
        # metadata worker with a direct proxy close. Leave the session open for
        # an owner-thread close retry after that worker completes.
        try:
            store_stopped = self._store._service_read_only_stopped is True
        except BaseException as error:
            report = _cleanup_report(failed=("store_state",), pending=("store_state",))
            self._record_cleanup_outcome(_CleanupOutcome(report))
            _attach_cleanup_report(error, report)
            if _is_control_interrupt(error):
                raise
            raise RuntimeAssemblyError("provider_cleanup_incomplete") from None
        if not store_stopped:
            try:
                metadata_probe_pending = self._metadata_probe_shutdown_pending()
            except BaseException as error:
                report = _cleanup_report(
                    failed=("metadata_probe_state",), pending=("metadata_probe_shutdown",)
                )
                self._record_cleanup_outcome(_CleanupOutcome(report))
                _attach_cleanup_report(error, report)
                if _is_control_interrupt(error):
                    raise
                raise RuntimeAssemblyError("provider_cleanup_incomplete") from None
            if metadata_probe_pending:
                report = _cleanup_report(pending=("metadata_probe_shutdown",))
                self._record_cleanup_outcome(_CleanupOutcome(report))
                error = RuntimeAssemblyError("provider_cleanup_incomplete")
                _attach_cleanup_report(error, report)
                raise error
            try:
                self._store.stop()
            except BaseException as cleanup_error:
                outcome = self._safe_compensate_failed_run()
                self._record_cleanup_outcome(outcome)
                _attach_cleanup_report(cleanup_error, outcome.report)
                if outcome.report.complete:
                    self._mark_closed_after_cleanup(outcome, primary=cleanup_error)
                if _is_control_interrupt(cleanup_error):
                    raise
                # The Store-stop exception is already the primary failure;
                # a control exception during compensation must not replace it.
                report = self._cleanup_report or outcome.report
                code = (
                    "provider_cleanup_failed" if report.complete else "run_cleanup_failed"
                )
                error = RuntimeAssemblyError(code)
                _attach_cleanup_report(error, report)
                raise error from None
            try:
                store_stopped = self._store._service_read_only_stopped is True
            except BaseException as error:
                report = _cleanup_report(failed=("store_state",), pending=("store_state",))
                self._record_cleanup_outcome(_CleanupOutcome(report))
                _attach_cleanup_report(error, report)
                if _is_control_interrupt(error):
                    raise
                raise RuntimeAssemblyError("provider_cleanup_incomplete") from None
            if not store_stopped:
                report = _cleanup_report(pending=("store_stop_unconfirmed",))
                self._record_cleanup_outcome(_CleanupOutcome(report))
                error = RuntimeAssemblyError("provider_cleanup_incomplete")
                _attach_cleanup_report(error, report)
                raise error
        try:
            proxy_connected = self._proxy.is_connected
        except BaseException as error:
            report = _cleanup_report(failed=("proxy_state",), pending=("proxy_state",))
            self._record_cleanup_outcome(_CleanupOutcome(report))
            _attach_cleanup_report(error, report)
            if _is_control_interrupt(error):
                raise
            raise RuntimeAssemblyError("provider_cleanup_incomplete") from None
        if proxy_connected is not False:
            report = _cleanup_report(pending=("proxy_disconnect",))
            self._record_cleanup_outcome(_CleanupOutcome(report))
            error = RuntimeAssemblyError("provider_cleanup_incomplete")
            _attach_cleanup_report(error, report)
            raise error
        outcome = _CleanupOutcome(
            _cleanup_report(confirmed=("store_stop", "proxy_disconnect"))
        )
        self._record_cleanup_outcome(outcome)
        self._mark_closed_after_cleanup(outcome)
        return strategies

    def close(self) -> None:
        self._assert_owner_thread()
        if self._closed:
            return
        if self._ran:
            outcome = self._safe_compensate_failed_run()
            self._record_cleanup_outcome(outcome)
            if outcome.report.complete:
                self._mark_closed_after_cleanup(
                    outcome, error_code="run_cleanup_failed"
                )
                return
            if outcome.interruption is not None:
                _attach_cleanup_report(outcome.interruption, outcome.report)
                raise outcome.interruption
            error = RuntimeAssemblyError("run_cleanup_failed")
            _attach_cleanup_report(error, outcome.report)
            raise error from None
        outcome = _disconnect_proxy(self._proxy)
        self._record_cleanup_outcome(outcome)
        if outcome.report.complete:
            self._mark_closed_after_cleanup(
                outcome, error_code="provider_cleanup_failed"
            )
            return
        if outcome.interruption is not None:
            _attach_cleanup_report(outcome.interruption, outcome.report)
            raise outcome.interruption
        error = RuntimeAssemblyError("provider_cleanup_failed")
        _attach_cleanup_report(error, outcome.report)
        raise error from None

    def __enter__(self):
        self._assert_owner_thread()
        if self._closed:
            raise RuntimeAssemblyError("session_closed")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            self._assert_owner_thread()
        except BaseException:
            if exc_value is None:
                raise
            report = _cleanup_report(pending=("cleanup_state_unknown",))
            self._record_cleanup_outcome(_CleanupOutcome(report))
            _attach_cleanup_report(exc_value, report)
            return False
        if not self._closed:
            if exc_value is None:
                self.close()
            elif self._cleanup_attempted:
                # run() may already have tried cleanup before its exception
                # reached the context manager. Do not repeat shutdown calls.
                if self._cleanup_report is not None:
                    _attach_cleanup_report(exc_value, self._cleanup_report)
            else:
                self._cleanup_preserving_primary(exc_value)
        return False


def assemble_run(
    source: object,
    registry: RuntimeBindings | RuntimeBindingsV2,
) -> RunSession:
    """Parse and revalidate one complete route before creating provider resources.

    The YAML input contains only I52 symbolic selectors. It cannot provide an
    import path, class, feed parameter, provider factory, API object, or local
    simulation option. All such bindings are supplied in application code.
    """

    config = parse_config(source)
    strategy_parameters = {}
    if type(config) is ValidatedRunConfig:
        contract_registry, bindings = _runtime_registry_parts(registry)
        plan = resolve_plan(config, contract_registry)
        validate_plan(plan, config, contract_registry)
        if config.plugins:
            raise RuntimeAssemblyError("plugins_unsupported")
        binding = _select_binding(plan, bindings)
    elif type(config) is ValidatedRunConfigV2:
        contract_registry, bindings = _runtime_registry_v2_parts(registry)
        plan = resolve_plan(config, contract_registry)
        validate_plan(plan, config, contract_registry)
        if not _REQUIRED_V2_READ_CAPABILITIES.issubset(plan.requested_capabilities):
            raise RuntimeAssemblyError("required_read_capability_missing")
        if any(enabled for _name, enabled in config.plugins):
            raise RuntimeAssemblyError("plugins_unsupported")
        binding = _select_binding_v2(plan, bindings)
        # Contract resolution validates names, top-level types, required
        # parameters, and the bounded nested tree. This API returns fresh
        # ordinary list/dict values suitable for Backtrader's kwargs adapter.
        strategy_parameters = config.strategy_parameters_dict()
        if type(strategy_parameters) is not dict or any(
            type(name) is not str for name in strategy_parameters
        ):
            raise RuntimeAssemblyError("invalid_strategy_parameters")
    else:  # parse_config currently returns only these two exact DTO types.
        raise RuntimeAssemblyError("unsupported_config_version")
    # Provider factories that expose a public preflight hook can bind their
    # route/config map to the exact route selected above. This hook runs before
    # the factory itself, so the SDK-owned factory can reject a mismatched
    # symbolic route before transport or registration starts. Legacy factories
    # without the hook retain their zero-argument contract and do not receive
    # this preflight guarantee. In either case this is consistency checking,
    # not provider identity or authorization.
    preflight = getattr(binding.provider_factory, "preflight", None)
    if callable(preflight):
        preflight_failed = False
        preflight_result = None
        try:
            preflight_result = preflight(binding.route)
        except Exception:  # noqa: BLE001 - expose only the finite preflight boundary code
            preflight_failed = True
        if preflight_failed or preflight_result is not True:
            raise RuntimeAssemblyError("provider_binding_preflight_failed") from None
    if not callable(getattr(ServiceReadProxy, "supports_bar_history", None)):
        raise RuntimeAssemblyError("backtrader_bar_history_capability_unavailable")

    provider_factory_failed = False
    proxy = None
    try:
        proxy = binding.provider_factory()
    except Exception:  # noqa: BLE001 - avoid propagating provider details through this boundary
        provider_factory_failed = True
    if provider_factory_failed:
        raise RuntimeAssemblyError("provider_factory_failed") from None
    if type(proxy) is not ServiceReadProxy:
        raise RuntimeAssemblyError("provider_factory_returned_unsupported_client") from None
    try:
        if (
            getattr(proxy, "service_read_only", None) is not True
            or getattr(proxy, "read_only", None) is not True
            or proxy.is_connected is not False
        ):
            raise RuntimeAssemblyError("provider_proxy_not_fresh_read_only")
        supports_bar_history = getattr(proxy, "supports_bar_history", None)
        if not callable(supports_bar_history):
            raise RuntimeAssemblyError("provider_bar_history_capability_unavailable")
        for feed in binding.feeds:
            if (
                supports_bar_history(
                    feed.dataname,
                    timeframe=TimeFrame.Minutes,
                    compression=1,
                )
                is not True
            ):
                raise RuntimeAssemblyError("provider_bar_history_binding_missing")
            if proxy.supports_live_streaming(feed.dataname) is not True:
                raise RuntimeAssemblyError("provider_feed_binding_missing")

        # This generic label is fixed; contract route strings never select an
        # SDK import or claim the identity of a real provider.
        store = BtApiStore(provider="btapi", api=proxy, backend="direct")
        broker = store.getbroker(market_data_only=True)
        if type(store) is not BtApiStore or type(broker) is not BtApiBroker:
            raise RuntimeAssemblyError("backtrader_adapter_type_mismatch")
        if broker._is_market_data_only() is not True:
            raise RuntimeAssemblyError("broker_not_read_only")

        cerebro = Cerebro(runonce=False, preload=False, live=True, writer=False, stdstats=False)
        cerebro.addstrategy(binding.strategy_class, **strategy_parameters)
        for spec in binding.feeds:
            feed = BtApiFeed(
                dataname=spec.dataname,
                store=store,
                timeframe=TimeFrame.Minutes,
                compression=1,
                backfill_start=True,
            )
            if type(feed) is not BtApiFeed:
                raise RuntimeAssemblyError("backtrader_adapter_type_mismatch")
            cerebro.adddata(feed, name=spec.dataname)
        cerebro.setbroker(broker)
        return RunSession(cerebro, store, broker, proxy, config)
    except BaseException as primary:
        outcome = _disconnect_proxy(proxy)
        _attach_cleanup_report(primary, outcome.report)
        raise


__all__ = [
    "FeedSpec",
    "RouteBinding",
    "RouteBindingV2",
    "RunSession",
    "RuntimeAssemblyError",
    "RuntimeBindings",
    "RuntimeBindingsV2",
    "RuntimeCleanupReport",
    "assemble_run",
]
