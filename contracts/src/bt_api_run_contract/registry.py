"""Pure in-memory binding of parsed references to caller-supplied route data.

The word "trusted" describes the deployment caller's assertion about registry
construction. These values do not prove provenance, grant permission, load a
provider, or authorize an order.
"""

from __future__ import annotations

from typing import Tuple, Union

from .errors import RunConfigError
from .parser import (
    Environment,
    References,
    ReferencesV2,
    RunMode,
    StrategyParameterList,
    StrategyParameterMap,
    ValidatedRunConfig,
    ValidatedRunConfigV2,
    _validate_requested_capability_items,
    _validate_strategy_parameter_items,
    _validate_strategy_parameter_name,
)


_REFERENCE_FIELDS = ("strategy", "provider", "endpoint", "account", "credential", "scope")
_V2_REFERENCE_FIELDS = (
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
_SENSITIVE_FIELDS = frozenset(("endpoint", "account", "credential", "scope"))
_V2_CAPABILITIES = frozenset(
    ("market.read", "account.read", "orders.paper.write", "orders.live.write")
)
_V2_WRITE_CAPABILITIES = frozenset(("orders.paper.write", "orders.live.write"))
_V2_PARAMETER_KINDS = frozenset(("null", "bool", "int", "float", "str", "list", "map"))
_PLAN_FACTORY_TOKEN = object()
_PLAN_V2_FACTORY_TOKEN = object()


def _raise(code: str) -> None:
    raise RunConfigError(code) from None


def _constructor_values(args: tuple, kwargs: dict, fields: Tuple[str, ...], code: str):
    if len(args) > len(fields):
        _raise(code)
    values = {}
    for name, value in zip(fields, args):
        values[name] = value
    for name, value in kwargs.items():
        if name not in fields or name in values:
            _raise(code)
        values[name] = value
    if len(values) != len(fields):
        _raise(code)
    return tuple(values[name] for name in fields)


def _reference_values(references: object, error_code: str) -> Tuple[str, ...]:
    """Copy and validate the six immutable symbolic values without retaining refs."""

    if type(references) is not References:
        _raise(error_code)
    try:
        References.__post_init__(references)
        values = tuple(object.__getattribute__(references, field) for field in _REFERENCE_FIELDS)
        References(*values)
    except RunConfigError:
        _raise(error_code)
    except (AttributeError, IndexError, TypeError):
        _raise(error_code)
    return values


def _reference_values_v2(references: object, error_code: str) -> Tuple[str, ...]:
    """Copy and validate the nine immutable version-2 symbolic values."""

    if type(references) is not ReferencesV2:
        _raise(error_code)
    try:
        ReferencesV2.__post_init__(references)
        values = tuple(
            object.__getattribute__(references, field) for field in _V2_REFERENCE_FIELDS
        )
        ReferencesV2(*values)
    except RunConfigError:
        _raise(error_code)
    except (AttributeError, IndexError, TypeError):
        _raise(error_code)
    return values


def _validate_v2_capabilities(capabilities: object, error_code: str) -> Tuple[str, ...]:
    if type(capabilities) is not tuple or len(capabilities) > 4:
        _raise(error_code)
    for capability in capabilities:
        if type(capability) is not str or capability not in _V2_CAPABILITIES:
            _raise(error_code)
    if capabilities != tuple(sorted(set(capabilities))):
        _raise(error_code)
    return capabilities


def _validate_parameter_contract(
    contract: object, error_code: str
) -> Tuple[Tuple[str, str, bool], ...]:
    """Validate a static, data-only strategy parameter descriptor."""

    if type(contract) is not tuple or len(contract) > 64:
        _raise(error_code)
    names = []
    for item in contract:
        if type(item) is not tuple or len(item) != 3:
            _raise(error_code)
        name, kind, required = item
        try:
            _validate_strategy_parameter_name(name)
        except RunConfigError:
            _raise(error_code)
        if type(kind) is not str or kind not in _V2_PARAMETER_KINDS:
            _raise(error_code)
        if type(required) is not bool:
            _raise(error_code)
        names.append(name)
    if names != sorted(set(names)):
        _raise(error_code)
    return contract


def _copy_parameter_value(value: object) -> object:
    """Copy the bounded immutable parameter tree without sharing wrappers."""

    if type(value) is StrategyParameterList:
        items = object.__getattribute__(value, "items")
        return StrategyParameterList(tuple(_copy_parameter_value(item) for item in items))
    if type(value) is StrategyParameterMap:
        entries = object.__getattribute__(value, "entries")
        return StrategyParameterMap(
            tuple((key, _copy_parameter_value(item)) for key, item in entries)
        )
    return value


def _copy_parameter_items(items: Tuple[Tuple[str, object], ...]) -> Tuple[Tuple[str, object], ...]:
    return tuple((name, _copy_parameter_value(value)) for name, value in items)


def _same_parameter_value(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is StrategyParameterList:
        left_items = object.__getattribute__(left, "items")
        right_items = object.__getattribute__(right, "items")
        return len(left_items) == len(right_items) and all(
            _same_parameter_value(left_item, right_item)
            for left_item, right_item in zip(left_items, right_items)
        )
    if type(left) is StrategyParameterMap:
        left_entries = object.__getattribute__(left, "entries")
        right_entries = object.__getattribute__(right, "entries")
        return len(left_entries) == len(right_entries) and all(
            left_key == right_key and _same_parameter_value(left_value, right_value)
            for (left_key, left_value), (right_key, right_value) in zip(
                left_entries, right_entries
            )
        )
    if type(left) is float:
        return left.hex() == right.hex()
    return left == right


def _same_parameter_items(
    left: Tuple[Tuple[str, object], ...], right: Tuple[Tuple[str, object], ...]
) -> bool:
    return len(left) == len(right) and all(
        left_name == right_name and _same_parameter_value(left_value, right_value)
        for (left_name, left_value), (right_name, right_value) in zip(left, right)
    )


def _parameter_kind(value: object) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "bool"
    if type(value) is int:
        return "int"
    if type(value) is float:
        return "float"
    if type(value) is str:
        return "str"
    if type(value) is StrategyParameterList:
        return "list"
    if type(value) is StrategyParameterMap:
        return "map"
    _raise("invalid_resolution_input")


def _validate_strategy_parameters(
    parameters: Tuple[Tuple[str, object], ...],
    contract: Tuple[Tuple[str, str, bool], ...],
) -> None:
    """Check declared names and exact top-level kinds on parser-validated data."""

    expected = {name: (kind, required) for name, kind, required in contract}
    supplied = {name: value for name, value in parameters}
    if any(name not in expected for name in supplied):
        _raise("unknown_strategy_parameter")
    if any(required and name not in supplied for name, _kind, required in contract):
        _raise("missing_strategy_parameter")
    for name, value in parameters:
        actual = _parameter_kind(value)
        expected_kind = expected[name][0]
        if actual != expected_kind:
            _raise("strategy_parameter_type_mismatch")


def _route_parts(route: object) -> Tuple[Environment, Tuple[str, ...]]:
    if type(route) is not TrustedRoute:
        _raise("invalid_route")
    try:
        if tuple.__len__(route) != 7:
            _raise("invalid_route")
        environment = tuple.__getitem__(route, 0)
        values = tuple.__getitem__(route, slice(1, None))
        if type(environment) is not Environment:
            _raise("invalid_route")
        References(*values)
    except RunConfigError:
        _raise("invalid_route")
    except (AttributeError, IndexError, TypeError):
        _raise("invalid_route")
    return environment, values


def _route_parts_v2(
    route: object,
) -> Tuple[Environment, Tuple[str, ...], Tuple[str, ...], Tuple[Tuple[str, str, bool], ...]]:
    if type(route) is not TrustedRouteV2:
        _raise("invalid_route")
    try:
        if tuple.__len__(route) != 12:
            _raise("invalid_route")
        environment = tuple.__getitem__(route, 0)
        values = tuple.__getitem__(route, slice(1, 10))
        capabilities = tuple.__getitem__(route, 10)
        parameter_contract = tuple.__getitem__(route, 11)
        if type(environment) is not Environment:
            _raise("invalid_route")
        ReferencesV2(*values)
        _validate_v2_capabilities(capabilities, "invalid_route")
        _validate_parameter_contract(parameter_contract, "invalid_route")
    except RunConfigError:
        _raise("invalid_route")
    except (AttributeError, IndexError, TypeError):
        _raise("invalid_route")
    return environment, values, capabilities, parameter_contract


def _validate_routes(routes: object) -> Tuple[Tuple[Environment, Tuple[str, ...]], ...]:
    if type(routes) is not tuple:
        _raise("invalid_registry")
    if not routes:
        _raise("empty_registry")

    seen_bindings = set()
    sensitive_environments = {}
    validated = []
    for route in routes:
        environment, values = _route_parts(route)
        binding = (environment, values)
        if binding in seen_bindings:
            _raise("duplicate_route")
        seen_bindings.add(binding)

        # These checks separate symbolic identifiers across paper/live. They
        # cannot prove what real object or account an identifier denotes.
        for field, reference in zip(_REFERENCE_FIELDS, values):
            if field not in _SENSITIVE_FIELDS:
                continue
            key = (field, reference)
            previous_environment = sensitive_environments.get(key)
            if previous_environment is not None and previous_environment is not environment:
                _raise("cross_environment_reference")
            sensitive_environments[key] = environment
        validated.append((environment, values))
    return tuple(validated)


def _validate_routes_v2(
    routes: object,
) -> Tuple[
    Tuple[Environment, Tuple[str, ...], Tuple[str, ...], Tuple[Tuple[str, str, bool], ...]],
    ...,
]:
    if type(routes) is not tuple:
        _raise("invalid_registry")
    if not routes:
        _raise("empty_registry")

    seen_bindings = set()
    sensitive_environments = {}
    validated = []
    for route in routes:
        environment, values, capabilities, parameter_contract = _route_parts_v2(route)
        binding = (environment, values)
        if binding in seen_bindings:
            _raise("duplicate_route")
        seen_bindings.add(binding)
        for field, reference in zip(_V2_REFERENCE_FIELDS, values):
            if field not in _SENSITIVE_FIELDS:
                continue
            key = (field, reference)
            previous_environment = sensitive_environments.get(key)
            if previous_environment is not None and previous_environment is not environment:
                _raise("cross_environment_reference")
            sensitive_environments[key] = environment
        validated.append((environment, values, capabilities, parameter_contract))
    return tuple(validated)


def _registry_routes(registry: object) -> Tuple[Tuple[Environment, Tuple[str, ...]], ...]:
    if type(registry) is not TrustedRegistry:
        _raise("invalid_registry")
    try:
        routes = tuple.__getitem__(registry, slice(None))
    except (AttributeError, IndexError, TypeError):
        _raise("invalid_registry")
    return _validate_routes(routes)


def _registry_routes_v2(
    registry: object,
) -> Tuple[
    Tuple[Environment, Tuple[str, ...], Tuple[str, ...], Tuple[Tuple[str, str, bool], ...]],
    ...,
]:
    if type(registry) is not TrustedRegistryV2:
        _raise("invalid_registry")
    try:
        routes = tuple.__getitem__(registry, slice(None))
    except (AttributeError, IndexError, TypeError):
        _raise("invalid_registry")
    return _validate_routes_v2(routes)


def _config_parts(
    config: object,
) -> Tuple[RunMode, Environment, Tuple[str, ...], Tuple[Tuple[str, bool], ...]]:
    if type(config) is not ValidatedRunConfig:
        _raise("invalid_resolution_input")
    try:
        references = object.__getattribute__(config, "references")
        values = _reference_values(references, "invalid_resolution_input")
        ValidatedRunConfig.__post_init__(config)
        mode = object.__getattribute__(config, "mode")
        environment = object.__getattribute__(config, "environment")
        plugins = object.__getattribute__(config, "plugins")
    except RunConfigError:
        raise
    except (AttributeError, IndexError, TypeError):
        _raise("invalid_resolution_input")
    return mode, environment, values, plugins


def _config_parts_v2(
    config: object,
) -> Tuple[
    RunMode,
    Environment,
    Tuple[str, ...],
    Tuple[Tuple[str, bool], ...],
    Tuple[Tuple[str, object], ...],
    Tuple[str, ...],
]:
    if type(config) is not ValidatedRunConfigV2:
        _raise("invalid_resolution_input")
    try:
        ValidatedRunConfigV2.__post_init__(config)
        mode = object.__getattribute__(config, "mode")
        environment = object.__getattribute__(config, "environment")
        references = object.__getattribute__(config, "references")
        plugins = object.__getattribute__(config, "plugins")
        parameters = object.__getattribute__(config, "strategy_parameters")
        capabilities = object.__getattribute__(config, "requested_capabilities")
        values = _reference_values_v2(references, "invalid_resolution_input")
        parameters = _copy_parameter_items(parameters)
        _validate_strategy_parameter_items(parameters)
        capabilities = _validate_v2_capabilities(capabilities, "invalid_resolution_input")
    except RunConfigError:
        _raise("invalid_resolution_input")
    except (AttributeError, IndexError, TypeError):
        _raise("invalid_resolution_input")
    return mode, environment, values, plugins, parameters, capabilities


def _plan_parts(plan: object) -> Tuple[RunMode, Environment, Tuple[str, ...]]:
    if type(plan) is not ResolvedPlan:
        _raise("invalid_resolution_input")
    try:
        values = tuple.__getitem__(plan, slice(None))
        if len(values) != 8:
            _raise("invalid_resolution_input")
        mode, environment = values[:2]
        references = values[2:]
        if (
            type(mode) is not RunMode
            or type(environment) is not Environment
            or mode.value != environment.value
        ):
            _raise("invalid_resolution_input")
        References(*references)
    except RunConfigError:
        _raise("invalid_resolution_input")
    except (AttributeError, IndexError, TypeError):
        _raise("invalid_resolution_input")
    return mode, environment, references


def _plan_parts_v2(
    plan: object,
) -> Tuple[
    RunMode,
    Environment,
    Tuple[str, ...],
    Tuple[str, ...],
    Tuple[Tuple[str, object], ...],
]:
    if type(plan) is not ResolvedPlanV2:
        _raise("invalid_resolution_input")
    try:
        values = tuple.__getitem__(plan, slice(None))
        if len(values) != 13:
            _raise("invalid_resolution_input")
        mode, environment = values[:2]
        references = values[2:11]
        capabilities = values[11]
        parameters = values[12]
        if (
            type(mode) is not RunMode
            or type(environment) is not Environment
            or mode.value != environment.value
        ):
            _raise("invalid_resolution_input")
        ReferencesV2(*references)
        _validate_v2_capabilities(capabilities, "invalid_resolution_input")
        _validate_requested_capability_items(mode, capabilities)
        _validate_strategy_parameter_items(parameters)
    except RunConfigError:
        _raise("invalid_resolution_input")
    except (AttributeError, IndexError, TypeError):
        _raise("invalid_resolution_input")
    return mode, environment, references, capabilities, parameters


def _select_route(
    environment: Environment,
    reference_values: Tuple[str, ...],
    routes: Tuple[Tuple[Environment, Tuple[str, ...]], ...],
) -> Tuple[Environment, Tuple[str, ...]]:
    provider = reference_values[1]
    provider_routes = tuple(row for row in routes if row[1][1] == provider)
    environment_routes = tuple(row for row in provider_routes if row[0] is environment)
    if not environment_routes:
        if provider_routes:
            _raise("route_environment_mismatch")
        _raise("unregistered_route")

    exact_routes = tuple(row for row in environment_routes if row[1] == reference_values)
    if not exact_routes:
        _raise("route_mismatch")
    if len(exact_routes) != 1:
        _raise("duplicate_route")
    return exact_routes[0]


class TrustedRoute(tuple):
    """One complete provider/environment binding, copied into tuple storage."""

    __slots__ = ()

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("TrustedRoute cannot be subclassed")

    def __new__(cls, *args: object, **kwargs: object):
        if cls is not TrustedRoute:
            _raise("invalid_route")
        environment, references = _constructor_values(
            args, kwargs, ("environment", "references"), "invalid_route"
        )
        if type(environment) is not Environment:
            _raise("invalid_route")
        values = _reference_values(references, "invalid_route")
        return tuple.__new__(cls, (environment,) + values)

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def __iter__(self):
        raise TypeError("trusted route values cannot be implicitly serialized")

    def __reduce__(self):
        raise TypeError("trusted route values cannot be pickled or copied")

    def __reduce_ex__(self, protocol: int):
        raise TypeError("trusted route values cannot be pickled or copied")

    @property
    def environment(self) -> Environment:
        return _route_parts(self)[0]

    @property
    def references(self) -> References:
        return References(*_route_parts(self)[1])

    def __repr__(self) -> str:
        return "TrustedRoute(<redacted>)"

    __str__ = __repr__


class TrustedRegistry(tuple):
    """A non-empty immutable set of unique complete route bindings.

    Multiple accounts/scopes may share a provider/environment. The full
    six-reference bundle must identify exactly one route. The caller remains
    responsible for supplying authoritative values; this class cannot prove
    their provenance.
    """

    __slots__ = ()

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("TrustedRegistry cannot be subclassed")

    def __new__(cls, *args: object, **kwargs: object):
        if cls is not TrustedRegistry:
            _raise("invalid_registry")
        (routes,) = _constructor_values(args, kwargs, ("routes",), "invalid_registry")
        _validate_routes(routes)
        return tuple.__new__(cls, routes)

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def __iter__(self):
        raise TypeError("trusted route values cannot be implicitly serialized")

    def __reduce__(self):
        raise TypeError("trusted route values cannot be pickled or copied")

    def __reduce_ex__(self, protocol: int):
        raise TypeError("trusted route values cannot be pickled or copied")

    @property
    def routes(self) -> Tuple[TrustedRoute, ...]:
        _registry_routes(self)
        return tuple.__getitem__(self, slice(None))

    def __repr__(self) -> str:
        return "TrustedRegistry(route_count={!r})".format(tuple.__len__(self))

    __str__ = __repr__


class TrustedRouteV2(tuple):
    """One complete version-2 route and its static declarations.

    Capabilities and parameter descriptors are caller-supplied code-owned
    data. They do not prove external identity or grant execution authority.
    """

    __slots__ = ()

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("TrustedRouteV2 cannot be subclassed")

    def __new__(cls, *args: object, **kwargs: object):
        if cls is not TrustedRouteV2:
            _raise("invalid_route")
        environment, references, capabilities, parameter_contract = _constructor_values(
            args,
            kwargs,
            (
                "environment",
                "references",
                "available_capabilities",
                "strategy_parameter_contract",
            ),
            "invalid_route",
        )
        if type(environment) is not Environment:
            _raise("invalid_route")
        reference_values = _reference_values_v2(references, "invalid_route")
        capabilities = _validate_v2_capabilities(capabilities, "invalid_route")
        parameter_contract = _validate_parameter_contract(parameter_contract, "invalid_route")
        return tuple.__new__(
            cls,
            (environment,) + reference_values + (capabilities, parameter_contract),
        )

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def __iter__(self):
        raise TypeError("trusted route values cannot be implicitly serialized")

    def __reduce__(self):
        raise TypeError("trusted route values cannot be pickled or copied")

    def __reduce_ex__(self, protocol: int):
        raise TypeError("trusted route values cannot be pickled or copied")

    @property
    def environment(self) -> Environment:
        return _route_parts_v2(self)[0]

    @property
    def references(self) -> ReferencesV2:
        return ReferencesV2(*_route_parts_v2(self)[1])

    @property
    def available_capabilities(self) -> Tuple[str, ...]:
        return _route_parts_v2(self)[2]

    @property
    def strategy_parameter_contract(self) -> Tuple[Tuple[str, str, bool], ...]:
        return _route_parts_v2(self)[3]

    def __repr__(self) -> str:
        return "TrustedRouteV2(<redacted>)"

    __str__ = __repr__


class TrustedRegistryV2(tuple):
    """Non-empty immutable collection of complete version-2 route bindings."""

    __slots__ = ()

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("TrustedRegistryV2 cannot be subclassed")

    def __new__(cls, *args: object, **kwargs: object):
        if cls is not TrustedRegistryV2:
            _raise("invalid_registry")
        (routes,) = _constructor_values(args, kwargs, ("routes",), "invalid_registry")
        _validate_routes_v2(routes)
        return tuple.__new__(cls, routes)

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def __iter__(self):
        raise TypeError("trusted route values cannot be implicitly serialized")

    def __reduce__(self):
        raise TypeError("trusted route values cannot be pickled or copied")

    def __reduce_ex__(self, protocol: int):
        raise TypeError("trusted route values cannot be pickled or copied")

    @property
    def routes(self) -> Tuple[TrustedRouteV2, ...]:
        _registry_routes_v2(self)
        return tuple.__getitem__(self, slice(None))

    def __repr__(self) -> str:
        return "TrustedRegistryV2(route_count={!r})".format(tuple.__len__(self))

    __str__ = __repr__


class ResolvedPlan(tuple):
    """Tuple-backed reference match; it carries no secret or execution authority.

    Ordinary construction is disabled. Python object mechanics are not a
    security boundary; consumers should call :func:`validate_plan` with the
    original config and registry immediately before using this static result.
    """

    __slots__ = ()

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ResolvedPlan cannot be subclassed")

    def __new__(cls, *args: object, **kwargs: object):
        _raise("direct_plan_construction")

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def __iter__(self):
        raise TypeError("resolved plan values cannot be implicitly serialized")

    def __reduce__(self):
        raise TypeError("resolved plan values cannot be pickled or copied")

    def __reduce_ex__(self, protocol: int):
        raise TypeError("resolved plan values cannot be pickled or copied")

    @classmethod
    def _from_binding(
        cls,
        token: object,
        mode: RunMode,
        environment: Environment,
        references: Tuple[str, ...],
    ) -> "ResolvedPlan":
        if token is not _PLAN_FACTORY_TOKEN or cls is not ResolvedPlan:
            _raise("direct_plan_construction")
        try:
            if (
                type(references) is not tuple
                or type(mode) is not RunMode
                or type(environment) is not Environment
                or mode.value != environment.value
            ):
                _raise("invalid_resolution_input")
            if len(references) != len(_REFERENCE_FIELDS):
                _raise("invalid_resolution_input")
            References(*references)
        except RunConfigError:
            _raise("invalid_resolution_input")
        except (AttributeError, IndexError, TypeError):
            _raise("invalid_resolution_input")
        return tuple.__new__(cls, (mode, environment) + references)

    @property
    def mode(self) -> RunMode:
        return _plan_parts(self)[0]

    @property
    def environment(self) -> Environment:
        return _plan_parts(self)[1]

    @property
    def references(self) -> References:
        return References(*_plan_parts(self)[2])

    def __repr__(self) -> str:
        try:
            mode, environment, _references = _plan_parts(self)
        except RunConfigError:
            return "ResolvedPlan(<invalid>)"
        return "ResolvedPlan(mode={!r}, environment={!r}, references=<redacted>)".format(
            mode, environment
        )

    __str__ = __repr__


class ResolvedPlanV2(tuple):
    """Non-authoritative static match for one version-2 config and route."""

    __slots__ = ()

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ResolvedPlanV2 cannot be subclassed")

    def __new__(cls, *args: object, **kwargs: object):
        _raise("direct_plan_construction")

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def __iter__(self):
        raise TypeError("resolved plan values cannot be implicitly serialized")

    def __reduce__(self):
        raise TypeError("resolved plan values cannot be pickled or copied")

    def __reduce_ex__(self, protocol: int):
        raise TypeError("resolved plan values cannot be pickled or copied")

    @classmethod
    def _from_binding(
        cls,
        token: object,
        mode: RunMode,
        environment: Environment,
        references: Tuple[str, ...],
        requested_capabilities: Tuple[str, ...],
        strategy_parameters: Tuple[Tuple[str, object], ...],
    ) -> "ResolvedPlanV2":
        if token is not _PLAN_V2_FACTORY_TOKEN or cls is not ResolvedPlanV2:
            _raise("direct_plan_construction")
        try:
            if (
                type(references) is not tuple
                or len(references) != len(_V2_REFERENCE_FIELDS)
                or type(mode) is not RunMode
                or type(environment) is not Environment
                or mode.value != environment.value
            ):
                _raise("invalid_resolution_input")
            ReferencesV2(*references)
            _validate_v2_capabilities(requested_capabilities, "invalid_resolution_input")
            _validate_requested_capability_items(mode, requested_capabilities)
            _validate_strategy_parameter_items(strategy_parameters)
            copied_parameters = _copy_parameter_items(strategy_parameters)
            _validate_strategy_parameter_items(copied_parameters)
        except RunConfigError:
            _raise("invalid_resolution_input")
        except (AttributeError, IndexError, TypeError):
            _raise("invalid_resolution_input")
        return tuple.__new__(
            cls,
            (mode, environment)
            + references
            + (requested_capabilities, copied_parameters),
        )

    @property
    def mode(self) -> RunMode:
        return _plan_parts_v2(self)[0]

    @property
    def environment(self) -> Environment:
        return _plan_parts_v2(self)[1]

    @property
    def references(self) -> ReferencesV2:
        return ReferencesV2(*_plan_parts_v2(self)[2])

    @property
    def requested_capabilities(self) -> Tuple[str, ...]:
        return _plan_parts_v2(self)[3]

    def __repr__(self) -> str:
        try:
            mode, environment, _references, _capabilities, _parameters = _plan_parts_v2(self)
        except RunConfigError:
            return "ResolvedPlanV2(<invalid>)"
        return "ResolvedPlanV2(mode={!r}, environment={!r}, references=<redacted>)".format(
            mode, environment
        )

    __str__ = __repr__


def resolve_plan(
    config: Union[ValidatedRunConfig, ValidatedRunConfigV2],
    registry: Union[TrustedRegistry, TrustedRegistryV2],
) -> Union[ResolvedPlan, ResolvedPlanV2]:
    """Match every config reference to one explicit in-memory route.

    This function performs only type checks and value comparisons. It does not
    import or call a strategy, provider, credential source, endpoint, factory,
    or plugin. V2 binds all nine references, checks only code-owned static
    capability/parameter declarations, and defers enabled plugins and write
    requests. No plan authorizes execution.
    """

    if type(config) is ValidatedRunConfigV2:
        mode, environment, references, plugins, parameters, capabilities = _config_parts_v2(config)
        routes = _registry_routes_v2(registry)
        route = _select_route(environment, references, routes)
        if any(enabled for _name, enabled in plugins):
            _raise("plugin_resolution_deferred")
        if any(capability in _V2_WRITE_CAPABILITIES for capability in capabilities):
            _raise("capability_resolution_deferred")
        if any(capability not in route[2] for capability in capabilities):
            _raise("capability_not_declared")
        _validate_strategy_parameters(parameters, route[3])
        return ResolvedPlanV2._from_binding(
            _PLAN_V2_FACTORY_TOKEN,
            mode,
            environment,
            references,
            capabilities,
            parameters,
        )
    if type(config) is not ValidatedRunConfig:
        _raise("invalid_resolution_input")
    mode, environment, references, plugins = _config_parts(config)
    routes = _registry_routes(registry)
    _select_route(environment, references, routes)
    if any(enabled for _name, enabled in plugins):
        _raise("plugin_resolution_deferred")
    return ResolvedPlan._from_binding(
        _PLAN_FACTORY_TOKEN,
        mode,
        environment,
        references,
    )


def validate_plan(
    plan: Union[ResolvedPlan, ResolvedPlanV2],
    config: Union[ValidatedRunConfig, ValidatedRunConfigV2],
    registry: Union[TrustedRegistry, TrustedRegistryV2],
) -> Union[ResolvedPlan, ResolvedPlanV2]:
    """Re-resolve the config and compare it with the plan before use.

    Reusing :func:`resolve_plan` applies the matching schema's route, plugin,
    parameter, and capability checks. This public guard confirms only a static
    contract match; it does not prove registry provenance, external identity,
    or execution authority.
    """

    if type(config) is ValidatedRunConfigV2:
        if type(plan) is not ResolvedPlanV2:
            _raise("plan_version_mismatch")
        try:
            plan_parts = _plan_parts_v2(plan)
            expected_parts = _plan_parts_v2(resolve_plan(config, registry))
            if plan_parts[:4] != expected_parts[:4] or not _same_parameter_items(
                plan_parts[4], expected_parts[4]
            ):
                _raise("plan_mismatch")
        except RunConfigError:
            raise
        except (AttributeError, IndexError, TypeError):
            _raise("invalid_resolution_input")
        return plan
    if type(config) is not ValidatedRunConfig:
        _raise("invalid_resolution_input")
    if type(plan) is not ResolvedPlan:
        _raise("plan_version_mismatch")
    try:
        plan_parts = _plan_parts(plan)
        expected_parts = _plan_parts(resolve_plan(config, registry))
        if plan_parts != expected_parts:
            _raise("plan_mismatch")
    except RunConfigError:
        raise
    except (AttributeError, IndexError, TypeError):
        _raise("invalid_resolution_input")
    return plan
