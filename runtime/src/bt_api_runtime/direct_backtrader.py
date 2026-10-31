"""Config-driven direct Backtrader paper/live session for iteration 52.

This module is intentionally imported only when a caller selects the direct
Backtrader topology.  The base ``bt_api_runtime`` package stays free of
Backtrader, SDK service and provider imports.

The session owns composition and lifecycle only.  Backtrader owns Cerebro,
Strategy, Feed and Broker; the Store/SDK own provider commands and order
facts.  A provider profile maps one trusted route reference to concrete SDK
venue keys and symbols.  Credentials and the write grant are injected by the
caller; this module never reads a private file, and no public value object
here grants provider or production permission.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from bt_api_run_contract import RunMode, ValidatedRunConfigV2

_TOPOLOGY_DIRECT = "direct"
_SUPPORTED_MODES = frozenset(("paper", "live"))
_RUNNABLE_ENVIRONMENTS = frozenset(("demo", "simulation", "paper", "production"))
_REQUIRED_READ_CAPABILITIES = frozenset(("market.read", "account.read"))
_WRITE_CAPABILITY_BY_MODE = {
    "paper": "orders.paper.write",
    "live": "orders.live.write",
}


class DirectSessionError(RuntimeError):
    """Finite, value-free failure from direct paper/live composition."""

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code.isascii() or not code:
            raise ValueError("code must be a non-empty ASCII string")
        self.code = code
        super().__init__(code)


def _require_positive_int(value: object, code: str) -> int:
    if isinstance(value, bool) or type(value) is not int or value <= 0:
        raise DirectSessionError(code)
    return value


def _require_positive_float(value: object, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise DirectSessionError(code)
    return float(value)


def _parse_utc(value: object, code: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise DirectSessionError(code)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise DirectSessionError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DirectSessionError(code)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class DirectProviderProfile:
    """Code-owned mapping from a trusted route reference to one SDK venue.

    ``credential_fields`` pairs each SDK exchange keyword with the injected
    credential name that supplies it.  Names only; values never enter this
    object.  ``supports_write`` is a static capability declaration, not an
    authorization.
    """

    provider_ref: str
    endpoint_ref: str
    account_ref: str
    credential_ref: str
    scope_ref: str
    sdk_route_key: str
    environment: str
    account_currency: str
    symbols: tuple[str, ...]
    credential_fields: tuple[tuple[str, str], ...]
    api_region: str | None = None
    position_mode: str = "dual_side"
    require_account_risk: bool = True
    account_maximum_loss_bps: str = "50"
    account_risk_max_age_seconds: str = "30"
    supports_write: bool = True
    data_timeframe: str = "ticks"
    readiness_quantity: float = 1.0
    extra_exchange_kwargs: tuple[tuple[str, object], ...] = ()
    extra_store_config: tuple[tuple[str, object], ...] = ()

    def __post_init__(self) -> None:
        for value, code in (
            (self.provider_ref, "invalid_provider_ref"),
            (self.endpoint_ref, "invalid_endpoint_ref"),
            (self.account_ref, "invalid_account_ref"),
            (self.credential_ref, "invalid_credential_ref"),
            (self.scope_ref, "invalid_scope_ref"),
            (self.sdk_route_key, "invalid_sdk_route_key"),
            (self.account_currency, "invalid_account_currency"),
        ):
            if type(value) is not str or not value or not value.isascii():
                raise DirectSessionError(code)
        if type(self.environment) is not str or self.environment not in _RUNNABLE_ENVIRONMENTS:
            raise DirectSessionError("invalid_provider_environment")
        if type(self.symbols) is not tuple or not self.symbols:
            raise DirectSessionError("invalid_symbols")
        if any(type(symbol) is not str or not symbol for symbol in self.symbols):
            raise DirectSessionError("invalid_symbols")
        if len(set(self.symbols)) != len(self.symbols):
            raise DirectSessionError("duplicate_symbol")
        if type(self.credential_fields) is not tuple or not self.credential_fields:
            raise DirectSessionError("invalid_credential_fields")
        for item in self.credential_fields:
            if (
                type(item) is not tuple
                or len(item) != 2
                or any(type(part) is not str or not part for part in item)
            ):
                raise DirectSessionError("invalid_credential_fields")
        if type(self.position_mode) is not str or not self.position_mode:
            raise DirectSessionError("invalid_position_mode")
        if type(self.data_timeframe) is not str or not self.data_timeframe:
            raise DirectSessionError("invalid_data_timeframe")
        _require_positive_float(self.readiness_quantity, "invalid_readiness_quantity")
        try:
            risk_age = float(self.account_risk_max_age_seconds)
        except (TypeError, ValueError) as exc:
            raise DirectSessionError("invalid_risk_max_age") from exc
        if not (0 < risk_age <= 300):
            raise DirectSessionError("invalid_risk_max_age")
        if type(self.require_account_risk) is not bool:
            raise DirectSessionError("invalid_risk_requirement")
        if type(self.supports_write) is not bool:
            raise DirectSessionError("invalid_write_capability")
        for extra in (self.extra_exchange_kwargs, self.extra_store_config):
            if type(extra) is not tuple or any(
                type(item) is not tuple or len(item) != 2 or type(item[0]) is not str
                for item in extra
            ):
                raise DirectSessionError("invalid_profile_extension")

    def exchange_kwargs(self, credential_values: Mapping[str, str]) -> dict[str, Any]:
        values: dict[str, Any] = {"environment": self.environment}
        if self.api_region is not None:
            values["api_region"] = self.api_region
        values.update(dict(self.extra_exchange_kwargs))
        for sdk_field, credential_name in self.credential_fields:
            secret = credential_values.get(credential_name)
            if type(secret) is not str or not secret:
                raise DirectSessionError("missing_credential_value")
            values[sdk_field] = secret
        return {self.sdk_route_key: values}


class DirectCredentials:
    """Injected credential values with a value-free representation."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str]) -> None:
        if not isinstance(values, Mapping):
            raise DirectSessionError("invalid_credentials")
        resolved: dict[str, str] = {}
        for name, value in values.items():
            if type(name) is not str or not name:
                raise DirectSessionError("invalid_credential_name")
            if type(value) is not str or not value:
                raise DirectSessionError("invalid_credential_value")
            resolved[name] = value
        self._values = resolved

    def __repr__(self) -> str:
        return "DirectCredentials(names={!r})".format(tuple(sorted(self._values)))

    def resolve(self, profile: DirectProviderProfile) -> dict[str, str]:
        for _, credential_name in profile.credential_fields:
            if credential_name not in self._values:
                raise DirectSessionError("missing_credential_value")
        return dict(self._values)


@dataclass(frozen=True)
class DirectWriteGrant:
    """Caller-injected bounded write authorization for one direct session.

    The object is validated but carries no signature or trusted-issuer proof.
    A deployment must obtain it from its own operator authorization boundary.
    """

    run_id: str
    mode: str
    provider_ref: str
    environment: str
    symbols: tuple[str, ...]
    expires_at_utc: str
    max_order_count: int
    max_order_notional: float
    allow_live_write: bool = False

    def __post_init__(self) -> None:
        if type(self.run_id) is not str or not self.run_id:
            raise DirectSessionError("invalid_grant_run_id")
        if type(self.mode) is not str or self.mode not in _SUPPORTED_MODES:
            raise DirectSessionError("invalid_grant_mode")
        if type(self.provider_ref) is not str or not self.provider_ref:
            raise DirectSessionError("invalid_grant_provider")
        if type(self.environment) is not str or not self.environment:
            raise DirectSessionError("invalid_grant_environment")
        if type(self.symbols) is not tuple or not self.symbols:
            raise DirectSessionError("invalid_grant_symbols")
        _parse_utc(self.expires_at_utc, "invalid_grant_expiry")
        _require_positive_int(self.max_order_count, "invalid_grant_order_count")
        _require_positive_float(self.max_order_notional, "invalid_grant_notional")
        if type(self.allow_live_write) is not bool:
            raise DirectSessionError("invalid_grant_live_flag")


@dataclass(frozen=True)
class DirectSessionReport:
    """Bounded, redacted session facts; not an order/provider authority."""

    status: str
    code: str
    run_id: str
    mode: str
    environment: str
    provider_ref: str
    topology: str
    symbols: tuple[str, ...]
    started: bool
    broker_started: bool
    store_started: bool
    readiness: Mapping[str, Any]
    execution_summary: Mapping[str, Any]
    shutdown: Mapping[str, Any]
    config_sha256: str
    strategy_facts: Mapping[str, Any] = field(default_factory=dict)
    cleanup_steps: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "code": self.code,
            "run_id": self.run_id,
            "mode": self.mode,
            "environment": self.environment,
            "provider_ref": self.provider_ref,
            "topology": self.topology,
            "symbols": list(self.symbols),
            "started": self.started,
            "broker_started": self.broker_started,
            "store_started": self.store_started,
            "readiness": _json_safe(self.readiness),
            "execution_summary": _json_safe(self.execution_summary),
            "shutdown": _json_safe(self.shutdown),
            "config_sha256": self.config_sha256,
            "strategy_facts": _json_safe(self.strategy_facts),
            "cleanup_steps": list(self.cleanup_steps),
        }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return repr(value)


def _timeframe(name: str):
    from backtrader.dataseries import TimeFrame

    try:
        return {
            "ticks": TimeFrame.Ticks,
            "seconds": TimeFrame.Seconds,
            "minutes": TimeFrame.Minutes,
            "daily": TimeFrame.Days,
        }[name]
    except KeyError as exc:
        raise DirectSessionError("unsupported_data_timeframe") from exc


def _validate_plan(plan: object, mode: str, environment: str) -> None:
    if not isinstance(plan, ValidatedRunConfigV2):
        raise DirectSessionError("invalid_run_plan")
    if plan.mode not in (RunMode.PAPER, RunMode.LIVE):
        raise DirectSessionError("deferred_run_mode")
    if plan.mode.value != mode:
        raise DirectSessionError("plan_mode_mismatch")
    if plan.environment.value != environment:
        raise DirectSessionError("plan_environment_mismatch")


def _validate_grant(
    grant: DirectWriteGrant,
    *,
    plan: ValidatedRunConfigV2,
    profile: DirectProviderProfile,
    run_id: str,
    symbols: Sequence[str],
    now: datetime,
) -> None:
    if grant.run_id != run_id:
        raise DirectSessionError("grant_run_id_mismatch")
    if grant.mode != plan.mode.value:
        raise DirectSessionError("grant_mode_mismatch")
    if grant.provider_ref != profile.provider_ref:
        raise DirectSessionError("grant_provider_mismatch")
    if grant.environment != profile.environment:
        raise DirectSessionError("grant_environment_mismatch")
    if tuple(grant.symbols) != tuple(symbols):
        raise DirectSessionError("grant_symbols_mismatch")
    if _parse_utc(grant.expires_at_utc, "invalid_grant_expiry") <= now:
        raise DirectSessionError("grant_expired")
    if plan.mode is RunMode.LIVE and not grant.allow_live_write:
        raise DirectSessionError("live_write_not_authorized")
    if plan.mode is RunMode.LIVE and profile.environment != "production":
        raise DirectSessionError("live_provider_environment_mismatch")
    if plan.mode is RunMode.PAPER and profile.environment == "production":
        raise DirectSessionError("paper_provider_environment_mismatch")


class DirectBacktraderSession:
    """Build, run and close one bounded direct Backtrader paper/live session."""

    def __init__(
        self,
        *,
        plan: ValidatedRunConfigV2,
        profile: DirectProviderProfile,
        run_id: str,
        credentials: DirectCredentials,
        strategy_id: str,
        grant: DirectWriteGrant,
        symbols: Sequence[str] | None = None,
        store_factory: Callable[..., Any] | None = None,
        broker_factory: Callable[..., Any] | None = None,
        cerebro_factory: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        initialize_risk_baseline: bool = False,
    ) -> None:
        if type(run_id) is not str or not run_id or not run_id.isascii():
            raise DirectSessionError("invalid_run_id")
        if type(strategy_id) is not str or not strategy_id or not strategy_id.isascii():
            raise DirectSessionError("invalid_strategy_id")
        if not isinstance(profile, DirectProviderProfile):
            raise DirectSessionError("invalid_provider_profile")
        if not isinstance(credentials, DirectCredentials):
            raise DirectSessionError("invalid_credentials")
        if not isinstance(grant, DirectWriteGrant):
            raise DirectSessionError("write_grant_required")
        selected = tuple(symbols if symbols is not None else profile.symbols)
        if not selected or any(symbol not in profile.symbols for symbol in selected):
            raise DirectSessionError("symbol_not_in_profile")
        self._plan = plan
        self._profile = profile
        self._run_id = run_id
        self._strategy_id = strategy_id
        self._symbols = selected
        self._credentials = credentials
        self._grant = grant
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._store_factory = store_factory
        self._broker_factory = broker_factory
        self._cerebro_factory = cerebro_factory
        self._initialize_risk_baseline = bool(initialize_risk_baseline)
        self._store: Any = None
        self._broker: Any = None
        self._store_started = False
        self._store_was_started = False
        self._broker_started = False
        self._closed = False
        _validate_plan(plan, plan.mode.value, plan.environment.value)
        if profile.environment == "production" and plan.mode is RunMode.PAPER:
            raise DirectSessionError("paper_provider_environment_mismatch")
        if profile.environment != "production" and plan.mode is RunMode.LIVE:
            raise DirectSessionError("live_provider_environment_mismatch")
        declared = frozenset(plan.requested_capabilities)
        if not _REQUIRED_READ_CAPABILITIES <= declared:
            raise DirectSessionError("read_capability_not_requested")
        if _WRITE_CAPABILITY_BY_MODE[plan.mode.value] not in declared:
            raise DirectSessionError("write_capability_not_requested")
        for profile_ref, plan_ref, code in (
            (profile.provider_ref, plan.references.provider, "plan_provider_mismatch"),
            (profile.endpoint_ref, plan.references.endpoint, "plan_endpoint_mismatch"),
            (profile.account_ref, plan.references.account, "plan_account_mismatch"),
            (profile.credential_ref, plan.references.credential, "plan_credential_mismatch"),
            (profile.scope_ref, plan.references.scope, "plan_scope_mismatch"),
        ):
            if profile_ref != plan_ref:
                raise DirectSessionError(code)

    @property
    def store(self) -> Any:
        return self._store

    @property
    def broker(self) -> Any:
        return self._broker

    def store_config(self) -> dict[str, Any]:
        if not self._profile.supports_write:
            raise DirectSessionError("provider_profile_is_read_only")
        credentials = self._credentials.resolve(self._profile)
        required_risk = self._profile.require_account_risk
        config: dict[str, Any] = {
            "exchange_kwargs": self._profile.exchange_kwargs(credentials),
            "symbol_routes": {symbol: self._profile.sdk_route_key for symbol in self._symbols},
            "market_data_only": False,
            "account_currency": self._profile.account_currency,
            "required_environments": {
                self._profile.sdk_route_key: self._profile.environment,
            },
            "strategy_id": self._strategy_id,
            "account_maximum_loss_bps": self._profile.account_maximum_loss_bps,
            "account_risk_max_age_seconds": self._profile.account_risk_max_age_seconds,
            "require_account_risk": required_risk,
        }
        config.update(dict(self._profile.extra_store_config))
        return config

    def config_sha256(self) -> str:
        payload = {
            "provider_ref": self._profile.provider_ref,
            "sdk_route_key": self._profile.sdk_route_key,
            "environment": self._profile.environment,
            "symbols": list(self._symbols),
            "position_mode": self._profile.position_mode,
            "strategy_id": self._strategy_id,
            "mode": self._plan.mode.value,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _build_store(self, config: Mapping[str, Any]) -> Any:
        if self._store_factory is not None:
            return self._store_factory(config=dict(config), profile=self._profile)
        from backtrader.stores.btapistore import BtApiStore

        return BtApiStore(provider="btapi", backend="direct", config=dict(config))

    def _readiness(self) -> dict[str, Any]:
        facts: dict[str, Any] = {}
        for symbol in self._symbols:
            environment = self._store.get_environment_info(symbol)
            account = self._store.get_account_config(symbol)
            if not isinstance(environment, Mapping):
                raise DirectSessionError("environment_unavailable")
            if not isinstance(account, Mapping):
                raise DirectSessionError("account_configuration_unavailable")
            if environment.get("environment") != self._profile.environment:
                raise DirectSessionError("provider_environment_mismatch")
            if account.get("can_trade") is not True:
                raise DirectSessionError("account_cannot_trade")
            remote_mode = account.get("position_mode")
            if remote_mode is not None and remote_mode != self._profile.position_mode:
                raise DirectSessionError("position_mode_mismatch")
            readiness = self._store.get_order_readiness(
                symbol,
                self._profile.readiness_quantity,
                position_mode=self._profile.position_mode,
            )
            if not isinstance(readiness, Mapping) or readiness.get("ready") is not True:
                raise DirectSessionError("order_readiness_failed")
            facts[symbol] = {
                "environment": environment.get("environment"),
                "can_trade": account.get("can_trade"),
                "position_mode": remote_mode,
                "order_ready": readiness.get("ready"),
            }
        return facts

    def preflight(self) -> dict[str, Any]:
        if self._closed:
            raise DirectSessionError("session_closed")
        if self._store is not None:
            raise DirectSessionError("session_already_preflighted")
        config = self.store_config()
        store = self._build_store(config)
        try:
            store.start()
            self._store = store
            self._store_started = True
            self._store_was_started = True
            if self._initialize_risk_baseline:
                initialize = getattr(store, "initialize_account_risk_baseline", None)
                if not callable(initialize):
                    raise DirectSessionError("risk_baseline_initializer_unavailable")
                initialize()
            return self._readiness()
        except Exception:
            self._close_store()
            raise

    def run(
        self,
        strategy_factory: Callable[..., Any],
        *,
        strategy_params: Mapping[str, Any] | None = None,
        seconds: float = 60.0,
        initialize_risk_baseline: bool | None = None,
    ) -> DirectSessionReport:
        if not callable(strategy_factory):
            raise DirectSessionError("invalid_strategy_factory")
        timeout = _require_positive_float(seconds, "invalid_session_duration")
        if initialize_risk_baseline is not None:
            self._initialize_risk_baseline = bool(initialize_risk_baseline)
        cleanup_steps: list[str] = []
        readiness: dict[str, Any] = {}
        execution_summary: dict[str, Any] = {}
        shutdown: dict[str, Any] = {}
        status = "FAILED"
        code = "unexpected_failure"
        cerebro = None
        timer = None
        strategy_facts: dict[str, Any] = {}
        try:
            _validate_grant(
                self._grant,
                plan=self._plan,
                profile=self._profile,
                run_id=self._run_id,
                symbols=self._symbols,
                now=self._clock(),
            )
            readiness = self.preflight()
            self._broker = self._build_broker()
            broker_start = getattr(self._broker, "start", None)
            if not callable(broker_start):
                raise DirectSessionError("broker_start_unavailable")
            broker_start()
            self._broker_started = True
            cerebro = self._build_cerebro(strategy_factory, strategy_params or {})
            deadline = time.monotonic() + timeout
            timer = _Deadline(
                deadline,
                lambda: getattr(cerebro, "runstop", None),
            )
            idle_pump = _IdlePump(cerebro)
            timer.start()
            idle_pump.start()
            try:
                run_result = cerebro.run()
            finally:
                idle_pump.stop()
                timer.cancel()
            strategy_facts = self._strategy_facts(run_result)
            execution_summary = self._execution_facts()
            status = "COMPLETED"
            code = "run_completed"
        except DirectSessionError as exc:
            code = exc.code
        except Exception as exc:  # noqa: BLE001 - finite code, no provider details
            code = "session_failure_" + type(exc).__name__[:40].lower()
        finally:
            if timer is not None:
                timer.cancel()
            shutdown = self._shutdown(cleanup_steps)
        return DirectSessionReport(
            status=status,
            code=code,
            run_id=self._run_id,
            mode=self._plan.mode.value,
            environment=self._profile.environment,
            provider_ref=self._profile.provider_ref,
            topology=_TOPOLOGY_DIRECT,
            symbols=self._symbols,
            started=self._store_was_started,
            broker_started=self._broker_started,
            store_started=self._store_was_started,
            readiness=readiness,
            execution_summary=execution_summary,
            shutdown=shutdown,
            config_sha256=self.config_sha256(),
            strategy_facts=strategy_facts,
            cleanup_steps=tuple(cleanup_steps),
        )

    def _build_broker(self) -> Any:
        if not self._profile.supports_write:
            raise DirectSessionError("provider_profile_is_read_only")
        kwargs = {
            "position_mode": self._profile.position_mode,
            "position_sync_policy": "startup",
            "position_audit_interval": 10.0,
            "shutdown_timeout": 30.0,
            "approval_expires_at_utc": self._grant.expires_at_utc,
            "approval_max_order_count": self._grant.max_order_count,
        }
        if self._broker_factory is not None:
            return self._broker_factory(store=self._store, profile=self._profile, **kwargs)
        return self._store.getbroker(**kwargs)

    def _build_cerebro(
        self,
        strategy_factory: Callable[..., Any],
        strategy_params: Mapping[str, Any],
    ) -> Any:
        if self._cerebro_factory is not None:
            cerebro = self._cerebro_factory()
        else:
            import backtrader as bt

            cerebro = bt.Cerebro(stdstats=False, quicknotify=True)
        cerebro.setbroker(self._broker)
        for symbol in self._symbols:
            data = self._store.getdata(
                dataname=symbol,
                timeframe=_timeframe(self._profile.data_timeframe),
                orderbook_as_ticks=self._profile.data_timeframe == "ticks",
                backfill_start=False,
                qcheck=0.01,
            )
            cerebro.adddata(data, name=symbol)
        cerebro.addstrategy(strategy_factory, **dict(strategy_params))
        return cerebro

    @staticmethod
    def _strategy_facts(run_result: Any) -> dict[str, Any]:
        candidates = run_result
        if isinstance(run_result, (list, tuple)) and run_result and isinstance(
            run_result[0], (list, tuple)
        ):
            candidates = run_result[0]
        if not isinstance(candidates, (list, tuple)) or not candidates:
            return {}
        facts_method = getattr(candidates[0], "session_facts", None)
        if not callable(facts_method):
            return {}
        try:
            facts = facts_method()
        except Exception:  # noqa: BLE001 - evidence stays finite
            return {"unavailable": True}
        return dict(facts) if isinstance(facts, Mapping) else {}

    def _execution_facts(self) -> dict[str, Any]:
        facts: dict[str, Any] = {}
        for label, owner, method_name in (
            ("broker", self._broker, "get_execution_summary"),
            ("store", self._store, "get_execution_summary"),
        ):
            method = getattr(owner, method_name, None)
            if callable(method):
                try:
                    facts[label] = method()
                except Exception:  # noqa: BLE001 - evidence stays finite
                    facts[label] = {"unavailable": True}
        return facts

    def _shutdown(self, steps: list[str]) -> dict[str, Any]:
        state: dict[str, Any] = {}
        if self._broker is not None:
            stop = getattr(self._broker, "stop", None)
            if callable(stop):
                try:
                    stop()
                    steps.append("broker.stop")
                    state["broker_stop"] = "returned"
                except Exception as exc:  # noqa: BLE001
                    steps.append("broker.stop.failed")
                    state["broker_stop"] = "failed:" + type(exc).__name__[:40]
            status = getattr(self._broker, "get_shutdown_state", None)
            if callable(status):
                try:
                    state["broker_shutdown_state"] = _json_safe(status())
                except Exception:  # noqa: BLE001
                    state["broker_shutdown_state"] = "unavailable"
        self._close_store(steps)
        if self._store is not None:
            state["store_connected"] = bool(getattr(self._store, "is_connected", False))
        self._closed = True
        return state

    def _close_store(self, steps: list[str] | None = None) -> None:
        if self._store is None or not self._store_started:
            return
        stop = getattr(self._store, "stop", None)
        if callable(stop):
            try:
                stop()
                if steps is not None:
                    steps.append("store.stop")
            except Exception as exc:  # noqa: BLE001
                if steps is not None:
                    steps.append("store.stop.failed:" + type(exc).__name__[:40])
        self._store_started = False

    def close(self) -> None:
        if self._closed:
            return
        self._shutdown([])


class _IdlePump(threading.Thread):
    """Drive strategy ``notify_idle`` while a live feed blocks the engine loop."""

    def __init__(self, cerebro: Any) -> None:
        super().__init__(name="bt-api-runtime-idle-pump", daemon=True)
        self._cerebro = cerebro
        self._stopped = threading.Event()

    def run(self) -> None:
        while not self._stopped.wait(0.5):
            try:
                running = getattr(self._cerebro, "runningstrats", None)
                groups = running or getattr(self._cerebro, "runstrats", None) or []
                flat = []
                for item in groups:
                    if isinstance(item, (list, tuple)):
                        flat.extend(item)
                    else:
                        flat.append(item)
                for strategy in flat:
                    notify = getattr(strategy, "notify_idle", None)
                    if callable(notify):
                        notify()
            except Exception:  # noqa: BLE001 - pump is bounded maintenance
                continue

    def stop(self) -> None:
        self._stopped.set()


class _Deadline(threading.Thread):
    def __init__(self, deadline: float, stop: Callable[[], Any] | None) -> None:
        super().__init__(name="bt-api-runtime-deadline", daemon=True)
        self._deadline = deadline
        self._stop = stop
        self._cancelled = threading.Event()

    def run(self) -> None:
        remaining = self._deadline - time.monotonic()
        if remaining > 0:
            self._cancelled.wait(remaining)
        if self._cancelled.is_set():
            return
        if callable(self._stop):
            try:
                self._stop()
            except Exception:  # noqa: BLE001 - deadline is best effort
                return

    def cancel(self) -> None:
        self._cancelled.set()
