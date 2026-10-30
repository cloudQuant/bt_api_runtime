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
    RunMode,
    ValidatedRunConfig,
    ValidatedRunConfigV2,
)


_REFERENCE_FIELDS = ("strategy", "provider", "endpoint", "account", "credential", "scope")
_SENSITIVE_FIELDS = frozenset(("endpoint", "account", "credential", "scope"))
_PLAN_FACTORY_TOKEN = object()


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


def _registry_routes(registry: object) -> Tuple[Tuple[Environment, Tuple[str, ...]], ...]:
    if type(registry) is not TrustedRegistry:
        _raise("invalid_registry")
    try:
        routes = tuple.__getitem__(registry, slice(None))
    except (AttributeError, IndexError, TypeError):
        _raise("invalid_registry")
    return _validate_routes(routes)


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


def resolve_plan(
    config: Union[ValidatedRunConfig, ValidatedRunConfigV2],
    registry: TrustedRegistry,
) -> ResolvedPlan:
    """Match every config reference to one explicit in-memory route.

    This function performs only type checks and value comparisons. It does not
    import or call a strategy, provider, credential source, endpoint, factory,
    or plugin. An enabled plugin is rejected until plugin resolution has its
    own registry and fail-closed availability/dependency contract.
    """

    if type(config) is ValidatedRunConfigV2:
        _raise("schema_v2_resolution_deferred")
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
    plan: ResolvedPlan,
    config: Union[ValidatedRunConfig, ValidatedRunConfigV2],
    registry: TrustedRegistry,
) -> ResolvedPlan:
    """Re-resolve the config and compare it with the plan before use.

    Reusing :func:`resolve_plan` applies the same config, environment,
    six-reference, and enabled-plugin checks. This public guard confirms only a
    static contract match; it does not prove registry provenance, external
    identity, or execution authority.
    """

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
