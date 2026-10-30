"""Pure-data run configuration contract, independent of runtime execution."""

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

__all__ = [
    "Environment",
    "MAX_CONFIG_BYTES",
    "References",
    "RunConfigError",
    "RunMode",
    "SCHEMA_VERSION",
    "ValidatedRunConfig",
    "parse_config",
]
