"""Code-owned composition of the SDK's read-only account and market facades.

The SDK is optional for importing ``bt_api_runtime``. This module imports only
the I52 route contract at module load time; SDK and Backtrader types are loaded
inside :meth:`SdkReadOnlyFacadeFactory.build` after an application requests an
SDK-backed run.

The caller supplies a typed SDK ClientConfig and an explicit mapping from every
I52 route reference to the corresponding SDK-side target. Those strings are
configuration selectors, not proof of provider identity or authorization.
The SDK's authenticated registrations and replies remain authoritative at
runtime.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable

from bt_api_run_contract import Environment, ReferencesV2, TrustedRouteV2


class SdkReadOnlyError(RuntimeError):
    """Finite, non-sensitive boundary error from SDK facade composition."""

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code.isascii() or not code:
            raise ValueError("code must be a non-empty ASCII string")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SdkRouteReference:
    """One explicit I52 symbolic reference to a code-bound SDK target."""

    route_ref: str
    sdk_ref: str

    def __repr__(self) -> str:
        return "SdkRouteReference(redacted=True)"


@dataclass(frozen=True)
class SdkRouteReferencesV2:
    """The nine explicit reference translations for one v2 route."""

    strategy: SdkRouteReference
    provider: SdkRouteReference
    endpoint: SdkRouteReference
    account: SdkRouteReference
    credential: SdkRouteReference
    scope: SdkRouteReference
    product: SdkRouteReference
    data_source: SdkRouteReference
    clock: SdkRouteReference

    def __repr__(self) -> str:
        return "SdkRouteReferencesV2(redacted=True)"


@dataclass(frozen=True)
class SdkEnvironmentMapping:
    """Explicit I52-environment to SDK-account-environment mapping."""

    route_environment: Environment
    sdk_environment: str

    def __repr__(self) -> str:
        return "SdkEnvironmentMapping(redacted=True)"


@dataclass(frozen=True)
class SdkClockBinding:
    """One immutable pairing of a route clock ref and code-owned clock leaf.

    This pairing records deployment intent. It does not calibrate the clock or
    prove that the provider's timestamps are authoritative.
    """

    sdk_ref: str
    read: Callable[[], Any]

    def __repr__(self) -> str:
        return "SdkClockBinding(redacted=True)"


@dataclass(frozen=True)
class SdkMarketSourceExpectation:
    """Code-owned source and clock identity expected from the SDK descriptor.

    These values are independent deployment declarations. They are never
    copied from a service response and establish local consistency only; they
    do not prove provider/source truth or clock calibration.
    """

    source: str
    clock_domain_id: str
    clock_source_session_id: str
    clock_source: str

    def __repr__(self) -> str:
        return "SdkMarketSourceExpectation(redacted=True)"


@dataclass(frozen=True)
class SdkReadOnlySpecV2:
    """Typed code-owned inputs for one account-plus-market read-only client.

    ``client_config`` must be an SDK ``ClientConfig`` returned by
    ``parse_client_config``. The nine reference pairs are deliberately
    separate from that config: the I52 grammar and SDK reference grammar need
    not match. The remaining fields bind those pairs to the exact SDK identity,
    account, endpoint, scopes, instrument, source and clock supplied by the
    deployment. ``market_source_expectation`` is an independent code-owned
    declaration checked against each descriptor returned by the SDK.

    ``transport_factory`` and ``subscriber_factory`` replace only the SDK's
    outer I/O leaves. The SDK runtime, account/market facades and
    ``ServiceReadProxy`` remain real.
    """

    route: TrustedRouteV2
    references: SdkRouteReferencesV2
    environment: SdkEnvironmentMapping
    client_config: object
    identity: object
    peer: object
    client_principal_ref: str
    service_principal_ref: str
    account_key: object
    account_resource: object
    instrument: object
    subscription_key: object
    account_scope: object
    market_scope: object
    stream_scope: object
    account_ref: str
    service_id: str
    instance_id: str
    account_scope_ref: str
    market_scope_ref: str
    transport_ref: str
    security_profile_ref: str
    rpc_endpoint: str
    market_endpoint: str
    venue: str
    product_type: str
    data_source_ref: str
    entitlement_ref: str
    market_source_expectation: SdkMarketSourceExpectation
    scope_context_field: str
    scope_context_ref: str
    clock_binding: SdkClockBinding
    data_name: str
    history_start_utc: str
    history_end_utc: str
    valuation_currency: str
    position_mode: str
    qty_unit: str
    transport_factory: Callable[[Any, Any], Any]
    subscriber_factory: Callable[[Any, Any], Any]

    def __repr__(self) -> str:
        return "SdkReadOnlySpecV2(redacted=True)"


_REFERENCE_FIELDS = (
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
_READ_ONLY_CAPABILITIES = frozenset(("account.read", "market.read"))
_ACCOUNT_OPERATIONS = frozenset(("account.snapshot",))
_MARKET_OPERATIONS = frozenset(
    (
        "market.subscribe",
        "market.unsubscribe",
        "market.snapshot",
        "market.head",
        "market.read_after",
        "market.history",
    )
)


def _load_sdk() -> dict[str, Any]:
    """Import optional SDK and Backtrader types only for an SDK-backed run."""

    failed = False
    try:
        from backtrader.stores.service_read_proxy import ServiceFeedBinding, ServiceReadProxy
        from bt_api_base.account_protocol import AccountKey, AccountResourceKey
        from bt_api_base.market_protocol import MarketInstrumentKey, MarketSubscriptionKey
        from bt_api_base.service_protocol import WireIdentity, WireRole, WireScope
        from bt_api_py.service_account import ServiceAccountClient
        from bt_api_py.service_config import ClientConfig
        from bt_api_py.service_market import (
            ExpectedMarketSourceIdentity,
            ServiceMarketClient,
        )
        from bt_api_py.service_runtime import ServiceClientRuntime
    except Exception:  # noqa: BLE001 - map optional import failures to one finite code
        failed = True
    if failed:
        raise SdkReadOnlyError("sdk_read_dependencies_unavailable") from None
    return {
        "AccountKey": AccountKey,
        "AccountResourceKey": AccountResourceKey,
        "ClientConfig": ClientConfig,
        "ExpectedMarketSourceIdentity": ExpectedMarketSourceIdentity,
        "MarketInstrumentKey": MarketInstrumentKey,
        "MarketSubscriptionKey": MarketSubscriptionKey,
        "ServiceAccountClient": ServiceAccountClient,
        "ServiceClientRuntime": ServiceClientRuntime,
        "ServiceFeedBinding": ServiceFeedBinding,
        "ServiceMarketClient": ServiceMarketClient,
        "ServiceReadProxy": ServiceReadProxy,
        "WireIdentity": WireIdentity,
        "WireRole": WireRole,
        "WireScope": WireScope,
    }


def _string(value: object) -> bool:
    return type(value) is str and bool(value) and "\x00" not in value


def _market_identity_string(value: object) -> bool:
    return _string(value) and value == value.strip()


def _is_empty_dimension(value: object) -> bool:
    if value is None or value is False or value == 0 or value == "":
        return True
    if type(value) in (tuple, list, set, frozenset, dict):
        return len(value) == 0
    return False


def _scope_matches(
    scope: object,
    *,
    context_field: str,
    context_ref: str,
    account_ref: str | None,
    strategy_ref: str | None,
) -> bool:
    """Check every typed scope dimension, including future/unknown fields."""

    try:
        from dataclasses import fields

        names = tuple(field.name for field in fields(scope))
        values = {name: getattr(scope, name) for name in names}
    except (AttributeError, TypeError):
        return False
    if (
        not names
        or context_field in {
            "account_ref",
            "account_refs",
            "strategy_ref",
            "run_id",
            "manual_book_ref",
            "portfolio_ref",
            "group_id",
            "leg_id",
        }
        or context_field not in values
        or values[context_field] != context_ref
    ):
        return False
    expected = {
        "account_ref": account_ref,
        "account_refs": (),
        "strategy_ref": strategy_ref,
        "run_id": None,
        "manual_book_ref": None,
        "portfolio_ref": None,
        "group_id": None,
        "leg_id": None,
    }
    if not set(expected).issubset(values):
        return False
    for name, target in expected.items():
        value = values[name]
        if name == "account_refs":
            if not _is_empty_dimension(value):
                return False
        elif value != target:
            return False
    # Any SDK scope field not covered by the known dimensions must be the
    # shared context field or an empty/default dimension. Unknown non-empty
    # fields are rejected until a later source round maps them explicitly.
    for name, value in values.items():
        if name in expected or name == context_field:
            continue
        if not _is_empty_dimension(value):
            return False
    return True


def _validate_config(spec: SdkReadOnlySpecV2, sdk: dict[str, Any]) -> dict[str, Any]:
    config = spec.client_config
    if type(config) is not sdk["ClientConfig"]:
        raise SdkReadOnlyError("client_config_type_mismatch") from None
    unreadable = False
    public = None
    try:
        public = config.to_public_dict()
    except Exception:  # noqa: BLE001 - keep malformed SDK config details out of the boundary
        unreadable = True
    if unreadable:
        raise SdkReadOnlyError("client_config_unreadable") from None
    if type(public) is not dict or public.get("process_role") != "strategy_client":
        raise SdkReadOnlyError("client_config_role_mismatch") from None
    registration = public.get("registration")
    if (
        type(registration) is not dict
        or set(registration) != {"strategy_ref"}
        or registration.get("strategy_ref") != spec.references.strategy.sdk_ref
    ):
        raise SdkReadOnlyError("client_config_identity_mismatch") from None
    clients = public.get("clients")
    if type(clients) is not dict or set(clients) != {"gateway"}:
        raise SdkReadOnlyError("client_config_mount_set_mismatch") from None
    gateway = clients["gateway"]
    if type(gateway) is not dict or set(gateway) != {"config_schema_version", "settings"}:
        raise SdkReadOnlyError("client_config_mount_set_mismatch") from None
    settings = gateway.get("settings")
    if type(settings) is not dict or set(settings) - {"bindings", "options"}:
        raise SdkReadOnlyError("client_config_mount_set_mismatch") from None
    rows = settings.get("bindings")
    if type(rows) is not list or len(rows) != 2:
        raise SdkReadOnlyError("client_config_mount_set_mismatch") from None
    by_mount: dict[str, dict[str, Any]] = {}
    for row in rows:
        if type(row) is not dict:
            raise SdkReadOnlyError("client_config_mount_set_mismatch") from None
        mount = row.get("mount")
        if type(mount) is not str or mount in by_mount:
            raise SdkReadOnlyError("client_config_mount_set_mismatch") from None
        by_mount[mount] = row
    if set(by_mount) != {"store.account", "store.market"}:
        raise SdkReadOnlyError("client_config_mount_set_mismatch") from None
    account = by_mount["store.account"]
    market = by_mount["store.market"]
    account_fields = {
        "mount",
        "service_id",
        "instance_id",
        "scope_ref",
        "transport_ref",
        "required",
        "account_ref",
    }
    market_fields = {
        "mount",
        "service_id",
        "instance_id",
        "scope_ref",
        "transport_ref",
        "required",
    }
    if set(account) != account_fields or set(market) != market_fields:
        raise SdkReadOnlyError("client_config_mount_set_mismatch") from None
    if account.get("required") is not True or market.get("required") is not True:
        raise SdkReadOnlyError("client_config_required_mount_mismatch") from None
    common = {
        "service_id": spec.service_id,
        "instance_id": spec.instance_id,
        "transport_ref": spec.references.endpoint.sdk_ref,
    }
    for row in (account, market):
        if any(row.get(name) != expected for name, expected in common.items()):
            raise SdkReadOnlyError("client_config_endpoint_mismatch") from None
    if (
        account.get("scope_ref") != spec.references.scope.sdk_ref
        or market.get("scope_ref") != spec.market_scope_ref
        or account.get("account_ref") != spec.references.account.sdk_ref
    ):
        raise SdkReadOnlyError("client_config_scope_mismatch") from None
    communication = public.get("communication")
    if type(communication) is not dict or set(communication) != {"config_schema_version", "profiles"}:
        raise SdkReadOnlyError("client_config_endpoint_mismatch") from None
    profiles = communication.get("profiles")
    if type(profiles) is not dict or set(profiles) != {spec.references.endpoint.sdk_ref}:
        raise SdkReadOnlyError("client_config_endpoint_mismatch") from None
    profile = profiles[spec.references.endpoint.sdk_ref]
    if type(profile) is not dict or set(profile) != {
        "backend",
        "config_schema_version",
        "endpoints",
        "security_profile_ref",
        "options",
    }:
        raise SdkReadOnlyError("client_config_endpoint_mismatch") from None
    endpoints = profile.get("endpoints")
    if (
        type(endpoints) is not dict
        or endpoints != {
            "rpc_connect": spec.rpc_endpoint,
            "market_connect": spec.market_endpoint,
        }
        or profile.get("security_profile_ref") != spec.references.credential.sdk_ref
    ):
        raise SdkReadOnlyError("client_config_endpoint_mismatch") from None
    if profile.get("backend") != "zmq":
        raise SdkReadOnlyError("client_config_backend_unsupported") from None
    return profile


def _validate_spec(spec: SdkReadOnlySpecV2, sdk: dict[str, Any]) -> dict[str, Any]:
    if type(spec) is not SdkReadOnlySpecV2:
        raise SdkReadOnlyError("invalid_sdk_read_spec") from None
    if type(spec.route) is not TrustedRouteV2 or type(spec.references) is not SdkRouteReferencesV2:
        raise SdkReadOnlyError("invalid_sdk_route_mapping") from None
    if type(spec.environment) is not SdkEnvironmentMapping:
        raise SdkReadOnlyError("invalid_sdk_environment_mapping") from None
    route = spec.route
    route_environment = route.environment
    route_references = route.references
    available = route.available_capabilities
    if (
        type(route_environment) is not Environment
        or spec.environment.route_environment is not route_environment
        or not _string(spec.environment.sdk_environment)
        or type(route_references) is not ReferencesV2
        or type(available) is not tuple
        or frozenset(available) != _READ_ONLY_CAPABILITIES
    ):
        raise SdkReadOnlyError("invalid_sdk_route_mapping") from None
    pairs = spec.references
    for name in _REFERENCE_FIELDS:
        pair = getattr(pairs, name, None)
        route_ref = getattr(route_references, name, None)
        if (
            type(pair) is not SdkRouteReference
            or not _string(pair.route_ref)
            or not _string(pair.sdk_ref)
            or pair.route_ref != route_ref
        ):
            raise SdkReadOnlyError("route_reference_mapping_mismatch") from None
    if any(
        not _string(value)
        for value in (
            spec.account_ref,
            spec.service_id,
            spec.instance_id,
            spec.client_principal_ref,
            spec.service_principal_ref,
            spec.account_scope_ref,
            spec.market_scope_ref,
            spec.transport_ref,
            spec.security_profile_ref,
            spec.rpc_endpoint,
            spec.market_endpoint,
            spec.venue,
            spec.product_type,
            spec.data_source_ref,
            spec.entitlement_ref,
            spec.data_name,
            spec.history_start_utc,
            spec.history_end_utc,
            spec.valuation_currency,
            spec.position_mode,
            spec.qty_unit,
            spec.scope_context_field,
            spec.scope_context_ref,
        )
    ):
        raise SdkReadOnlyError("invalid_sdk_read_mapping") from None
    if (
        type(spec.clock_binding) is not SdkClockBinding
        or not _string(spec.clock_binding.sdk_ref)
        or not callable(spec.clock_binding.read)
        or not callable(spec.transport_factory)
        or not callable(spec.subscriber_factory)
    ):
        raise SdkReadOnlyError("invalid_sdk_read_factory") from None
    expected_source = spec.market_source_expectation
    if (
        type(expected_source) is not SdkMarketSourceExpectation
        or any(
            not _market_identity_string(value)
            for value in (
                getattr(expected_source, "source", None),
                getattr(expected_source, "clock_domain_id", None),
                getattr(expected_source, "clock_source_session_id", None),
                getattr(expected_source, "clock_source", None),
            )
        )
        or not isinstance(sdk.get("ExpectedMarketSourceIdentity"), type)
    ):
        raise SdkReadOnlyError("invalid_sdk_market_source_expectation") from None
    identity = spec.identity
    peer = spec.peer
    account_key = spec.account_key
    account_resource = spec.account_resource
    instrument = spec.instrument
    subscription_key = spec.subscription_key
    account_scope = spec.account_scope
    market_scope = spec.market_scope
    stream_scope = spec.stream_scope
    if (
        type(identity) is not sdk["WireIdentity"]
        or identity.role is not sdk["WireRole"].STRATEGY_CLIENT
        or identity.actor_ref != pairs.strategy.sdk_ref
        or identity.principal_ref != spec.client_principal_ref
        or identity.delegate_ref is not None
        or type(peer) is not sdk["WireIdentity"]
        or peer.role is not sdk["WireRole"].SERVICE
        or peer.actor_ref != spec.service_id
        or peer.principal_ref != spec.service_principal_ref
        or peer.delegate_ref is not None
        or type(account_key) is not sdk["AccountKey"]
        or type(account_resource) is not sdk["AccountResourceKey"]
        or account_resource.product_type != spec.product_type
        or type(instrument) is not sdk["MarketInstrumentKey"]
        or type(subscription_key) is not sdk["MarketSubscriptionKey"]
        or subscription_key.instrument != instrument
        or type(account_scope) is not sdk["WireScope"]
        or type(market_scope) is not sdk["WireScope"]
        or type(stream_scope) is not sdk["WireScope"]
    ):
        raise SdkReadOnlyError("sdk_identity_or_domain_type_mismatch") from None
    if (
        pairs.strategy.sdk_ref != identity.actor_ref
        or pairs.provider.sdk_ref != account_key.provider
        or pairs.provider.sdk_ref != instrument.provider
        or account_key.venue != spec.venue
        or instrument.venue != spec.venue
        or account_key.environment != spec.environment.sdk_environment
        or instrument.environment != spec.environment.sdk_environment
        or pairs.account.sdk_ref != spec.account_ref
        or account_key.account_id != spec.account_ref
        or pairs.product.sdk_ref != spec.product_type
        or instrument.product_type != spec.product_type
        or pairs.data_source.sdk_ref != spec.data_source_ref
        or subscription_key.source_ref != spec.data_source_ref
        or subscription_key.entitlement_ref != spec.entitlement_ref
        or pairs.clock.sdk_ref != spec.clock_binding.sdk_ref
        or pairs.endpoint.sdk_ref != spec.transport_ref
        or pairs.credential.sdk_ref != spec.security_profile_ref
        or pairs.scope.sdk_ref != spec.account_scope_ref
    ):
        raise SdkReadOnlyError("sdk_route_binding_mismatch") from None
    if (
        not _scope_matches(
            account_scope,
            context_field=spec.scope_context_field,
            context_ref=spec.scope_context_ref,
            account_ref=spec.account_ref,
            strategy_ref=identity.actor_ref,
        )
        or not _scope_matches(
            market_scope,
            context_field=spec.scope_context_field,
            context_ref=spec.scope_context_ref,
            account_ref=None,
            strategy_ref=identity.actor_ref,
        )
        or not _scope_matches(
            stream_scope,
            context_field=spec.scope_context_field,
            context_ref=spec.scope_context_ref,
            account_ref=None,
            strategy_ref=None,
        )
        or subscription_key.channel != "bar"
        or subscription_key.timeframe != "1m"
        or instrument.native_symbol != spec.data_name
    ):
        raise SdkReadOnlyError("sdk_scope_or_market_mapping_mismatch") from None
    if spec.account_scope_ref == spec.market_scope_ref:
        raise SdkReadOnlyError("sdk_scope_mapping_ambiguous") from None
    profile = _validate_config(spec, sdk)
    return profile


class SdkReadOnlyResourceOwner:
    """Owner-thread retention and ordered close for one SDK facade group.

    The owner keeps strong references to every successfully constructed
    resource. It closes the proxy/facades first, and calls the shared SDK
    runtime's close only after that facade boundary returns successfully. A
    failed or uncertain close leaves the whole group available for an explicit
    owner-thread retry. Remote uncertainty is reported as unresolved; retry is
    an opportunity, not a promise that the remote service will confirm it.
    """

    __slots__ = (
        "_account_client",
        "_closed",
        "_market_client",
        "_owner_thread_id",
        "_proxy",
        "_runtime",
        "_state",
    )

    def __init__(self) -> None:
        self._owner_thread_id = threading.get_ident()
        self._runtime = None
        self._account_client = None
        self._market_client = None
        self._proxy = None
        self._closed = False
        self._state = "building"

    @property
    def runtime(self):
        return self._runtime

    @property
    def account_client(self):
        return self._account_client

    @property
    def market_client(self):
        return self._market_client

    @property
    def proxy(self):
        return self._proxy

    @property
    def state(self) -> str:
        return self._state

    @property
    def retained(self) -> bool:
        return self._state in {"close_failed", "remote_uncertain"}

    def _assert_owner_thread(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise SdkReadOnlyError("sdk_owner_wrong_thread") from None
    def _close_facades(self) -> None:
        failed = False
        if self._proxy is not None:
            try:
                self._proxy.disconnect()
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                self._state = "close_failed"
                raise
            except Exception:  # noqa: BLE001 - retain the whole owner after facade-close failure
                failed = True
        else:
            for facade in (self._market_client, self._account_client):
                if facade is None:
                    continue
                try:
                    facade.close()
                except (KeyboardInterrupt, SystemExit, GeneratorExit):
                    self._state = "close_failed"
                    raise
                except Exception:  # noqa: BLE001 - attempt the remaining owned facade closes
                    failed = True
        if failed:
            self._state = "close_failed"
            raise SdkReadOnlyError("sdk_facade_close_failed") from None
    def _runtime_diagnostics(self) -> tuple[str, int, int, int] | None:
        runtime = self._runtime
        if runtime is None:
            return ("absent", 0, 0, 0)
        try:
            state = runtime.state
            cleanup_failures = runtime.cleanup_failures
            unregister_uncertainties = runtime.unregister_uncertainties
            unknown_registration_count = runtime.unknown_registration_count
        except Exception:  # noqa: BLE001 - public cleanup counters become unobserved
            return None
        values = (cleanup_failures, unregister_uncertainties, unknown_registration_count)
        if (
            type(state) is not str
            or any(type(value) is not int or value < 0 for value in values)
        ):
            return None
        return state, cleanup_failures, unregister_uncertainties, unknown_registration_count

    def close(self) -> None:
        """Close the facade group, then its runtime; retain on any uncertainty."""

        self._assert_owner_thread()
        if self._closed:
            return
        self._close_facades()

        market = self._market_client
        if market is not None:
            try:
                uncertainty = market.unsubscription_uncertain
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                self._state = "remote_uncertain"
                raise
            except Exception:  # noqa: BLE001 - a failed public read is unresolved, not false
                uncertainty = None
            if type(uncertainty) is not bool or uncertainty:
                self._state = "remote_uncertain"
                raise SdkReadOnlyError("sdk_market_unsubscribe_uncertain") from None
        try:
            before = self._runtime_diagnostics()
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            self._state = "remote_uncertain"
            raise
        if before is None:
            self._state = "remote_uncertain"
            raise SdkReadOnlyError("sdk_runtime_cleanup_unobserved") from None
        if before[2] or before[3]:
            # Preserve the runtime transport so an owner may investigate or
            # retry the SDK's explicit unregister path. Local close is not
            # evidence of remote registration removal.
            self._state = "remote_uncertain"
            raise SdkReadOnlyError("sdk_remote_registration_uncertain") from None
        if self._runtime is not None:
            runtime_close_failed = False
            try:
                self._runtime.close()
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                self._state = "close_failed"
                raise
            except Exception:  # noqa: BLE001 - preserve visible runtime for owner retry
                runtime_close_failed = True
            if runtime_close_failed:
                self._state = "close_failed"
                raise SdkReadOnlyError("sdk_runtime_close_failed") from None
        try:
            after = self._runtime_diagnostics()
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            self._state = "remote_uncertain"
            raise
        if after is None:
            self._state = "remote_uncertain"
            raise SdkReadOnlyError("sdk_runtime_cleanup_unobserved") from None
        if after[0] not in {"closed", "absent"} or any(after[1:]):
            self._state = "remote_uncertain"
            raise SdkReadOnlyError("sdk_runtime_cleanup_unconfirmed") from None
        self._state = "closed"
        self._closed = True


class SdkReadOnlyFacadeFactory:
    """One-shot factory bound to exact route/config and injected SDK leaves.

    The returned :class:`SdkReadOnlyResourceOwner` owns the SDK runtime and all
    facades. A ``RunSession`` may use the returned proxy for one ordinary run;
    it closes the per-run proxy boundary but borrows the runtime. The caller
    then calls ``owner.close()`` on the same thread to close the SDK runtime.
    The factory keeps its owner visible if construction fails partway through.
    """

    __slots__ = (
        "_owner",
        "_owner_thread_id",
        "_profile",
        "_sdk",
        "_spec",
        "_used",
    )

    def __init__(self, spec: SdkReadOnlySpecV2) -> None:
        if type(spec) is not SdkReadOnlySpecV2:
            raise SdkReadOnlyError("invalid_sdk_read_spec") from None
        self._spec = spec
        self._owner = SdkReadOnlyResourceOwner()
        self._owner_thread_id = threading.get_ident()
        self._profile = None
        self._sdk = None
        self._used = False

    @property
    def owner(self) -> SdkReadOnlyResourceOwner:
        """The visible owner, including any resources from partial setup."""

        return self._owner

    def __call__(self):
        """Return the fresh proxy expected by ``RuntimeBindingsV2``."""

        return self.build().proxy

    def preflight(self, selected_route: object) -> bool:
        """Validate this spec against the route selected by ``assemble_run``.

        This is a consistency check before the provider factory. It is not a
        provider identity check, credential validation, or authorization.
        """

        if threading.get_ident() != self._owner_thread_id:
            raise SdkReadOnlyError("sdk_factory_wrong_thread") from None
        if self._used:
            raise SdkReadOnlyError("sdk_factory_already_used") from None
        if type(selected_route) is not TrustedRouteV2 or selected_route != self._spec.route:
            raise SdkReadOnlyError("sdk_selected_route_mismatch") from None
        sdk = _load_sdk()
        self._profile = _validate_spec(self._spec, sdk)
        self._sdk = sdk
        return True

    def build(self) -> SdkReadOnlyResourceOwner:
        if threading.get_ident() != self._owner_thread_id:
            raise SdkReadOnlyError("sdk_factory_wrong_thread") from None
        if self._used:
            raise SdkReadOnlyError("sdk_factory_already_used") from None
        if self._profile is None:
            self.preflight(self._spec.route)
        self._used = True

        # All reference, environment, identity, account, endpoint, scope,
        # product, data-source, source/clock expectation, capability, and
        # ClientConfig checks run before constructing the SDK runtime or calling
        # an I/O factory.
        sdk = self._sdk
        profile = self._profile
        if type(profile) is not dict or type(sdk) is not dict:
            raise SdkReadOnlyError("sdk_factory_preflight_unavailable") from None
        spec = self._spec

        def scope_resolver(scope_ref: str):
            if scope_ref == spec.account_scope_ref:
                return spec.account_scope
            if scope_ref == spec.market_scope_ref:
                return spec.market_scope
            raise KeyError(scope_ref)

        def peer_resolver(service_id: str):
            return spec.peer if service_id == spec.service_id else None

        expected_profile = dict(profile)

        def transport_factory(actual_profile: Any, actual_peer: Any):
            if actual_profile != expected_profile or actual_peer is not spec.peer:
                raise SdkReadOnlyError("sdk_transport_binding_changed") from None
            return spec.transport_factory(actual_profile, actual_peer)

        owner = self._owner
        failure_code = None
        process_control_error = None
        try:
            expected_source = spec.market_source_expectation
            expected_source_identity = sdk["ExpectedMarketSourceIdentity"](
                source=expected_source.source,
                clock_domain_id=expected_source.clock_domain_id,
                clock_source_session_id=expected_source.clock_source_session_id,
                clock_source=expected_source.clock_source,
            )
            runtime = sdk["ServiceClientRuntime"](
                spec.client_config,
                identity=spec.identity,
                scope_resolver=scope_resolver,
                peer_resolver=peer_resolver,
                transport_factory=transport_factory,
                requested_operations={
                    "store.account": _ACCOUNT_OPERATIONS,
                    "store.market": _MARKET_OPERATIONS,
                },
            )
            owner._runtime = runtime

            account_client = sdk["ServiceAccountClient"](
                runtime,
                "store.account",
                account_ref=spec.references.account.sdk_ref,
                expected_account=spec.account_key,
                required_coverage=(spec.account_resource,),
                require_open_orders=True,
                expected_scope_ref=spec.account_scope_ref,
            )
            owner._account_client = account_client

            market_client = sdk["ServiceMarketClient"](
                runtime,
                "store.market",
                spec.subscription_key,
                subscriber_factory=spec.subscriber_factory,
                stream_scope=spec.stream_scope,
                # Require the SDK's typed source descriptor on subscribe.
                # The expected source/clock identity is independently supplied
                # by code-owned configuration; its match check is still only
                # local consistency, not proof of provider/source truth.
                require_source_descriptor=True,
                expected_source_identity=expected_source_identity,
            )
            owner._market_client = market_client

            proxy = sdk["ServiceReadProxy"](
                account_client,
                {
                    spec.data_name: sdk["ServiceFeedBinding"](
                        market_client=market_client,
                        resource=spec.account_resource,
                        qty_unit=spec.qty_unit,
                        timeframe="1m",
                        history_start_utc=spec.history_start_utc,
                        history_end_utc=spec.history_end_utc,
                    )
                },
                valuation_currency=spec.valuation_currency,
                position_mode=spec.position_mode,
                clock=spec.clock_binding.read,
            )
            owner._proxy = proxy

            # ServiceClientRuntime.start is the first operation that may call
            # transport_factory; the complete static map has already passed.
            runtime.start()
            owner._state = "ready"
        except Exception:  # noqa: BLE001 - map SDK/setup details to one finite public error
            failure_code = "sdk_facade_construction_failed"
        except BaseException as error:  # noqa: BLE001 - clean up, then propagate controls unchanged
            # Preserve process-control signals. Cleanup is attempted below,
            # while all partially created SDK resources remain visible on the
            # owner if that cleanup cannot be confirmed.
            process_control_error = error
        if process_control_error is not None:
            if owner.runtime is not None:
                try:
                    owner.close()
                except BaseException:  # noqa: BLE001 - the original control signal remains primary
                    if owner._state not in {"closed", "close_failed", "remote_uncertain"}:
                        owner._state = "remote_uncertain"
            raise process_control_error from None
        if failure_code is not None:
            if owner.runtime is not None:
                try:
                    owner.close()
                except SdkReadOnlyError:
                    pass
            raise SdkReadOnlyError(failure_code) from None
        return owner


__all__ = [
    "SdkClockBinding",
    "SdkEnvironmentMapping",
    "SdkMarketSourceExpectation",
    "SdkReadOnlyError",
    "SdkReadOnlyFacadeFactory",
    "SdkReadOnlyResourceOwner",
    "SdkReadOnlySpecV2",
    "SdkRouteReference",
    "SdkRouteReferencesV2",
]
