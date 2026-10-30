"""Pure-data config and static route matching, independent of execution authority."""

from .errors import RunConfigError
from .parser import (
    MAX_CONFIG_BYTES,
    SCHEMA_VERSION,
    Environment,
    References,
    RunMode,
    ValidatedRunConfig,
    parse_config,
)
from .registry import ResolvedPlan, TrustedRegistry, TrustedRoute, resolve_plan, validate_plan

__all__ = [
    "Environment",
    "MAX_CONFIG_BYTES",
    "References",
    "RunConfigError",
    "RunMode",
    "ResolvedPlan",
    "SCHEMA_VERSION",
    "TrustedRegistry",
    "TrustedRoute",
    "ValidatedRunConfig",
    "parse_config",
    "resolve_plan",
    "validate_plan",
]
