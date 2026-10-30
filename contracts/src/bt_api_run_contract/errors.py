"""Safe, value-free errors for the run configuration contract."""

from typing import Dict


_MESSAGES: Dict[str, str] = {
    "invalid_input_type": "Config must be text or UTF-8 bytes.",
    "input_too_large": "Config exceeds the maximum input size.",
    "invalid_utf8": "Config must be valid UTF-8 text.",
    "invalid_yaml": "Config YAML is invalid or uses an unsupported feature.",
    "duplicate_key": "Config contains a duplicate mapping key.",
    "multiple_documents": "Config must contain exactly one YAML document.",
    "document_too_complex": "Config exceeds the YAML complexity limit.",
    "invalid_schema": "Config must be a mapping that follows the supported schema.",
    "missing_field": "Config is missing a required field.",
    "unknown_field": "Config contains an unknown field.",
    "invalid_schema_version": "Config schema version is invalid.",
    "unsupported_schema_version": "Config schema version is not supported.",
    "schema_v2_resolution_deferred": (
        "Schema version 2 resolution is deferred; no executable plan is produced."
    ),
    "invalid_mode": "Config mode is invalid.",
    "mode_deferred": "This mode is recognized but deferred and cannot produce a run config.",
    "invalid_environment": "Config environment is invalid.",
    "environment_mismatch": "Config mode and environment do not match.",
    "invalid_references": "Config references must be a mapping of symbolic identifiers.",
    "invalid_reference": "Config contains an invalid symbolic reference.",
    "invalid_strategy_parameters": (
        "Strategy parameters must be a bounded mapping of pure-data values."
    ),
    "strategy_parameters_too_large": "Config contains too many strategy parameters.",
    "invalid_strategy_parameter_name": "Config contains an invalid strategy parameter name.",
    "invalid_strategy_parameter_value": (
        "Config contains an invalid or out-of-bounds strategy parameter value."
    ),
    "invalid_capabilities": (
        "Requested capabilities must be a bounded sequence of known identifiers."
    ),
    "unknown_capability": "Config requests an unsupported capability.",
    "duplicate_capability": "Config requests a capability more than once.",
    "capability_mode_mismatch": (
        "A requested write capability does not match the selected mode."
    ),
    "invalid_plugins": "Config plugins must be a mapping of symbolic identifiers to booleans.",
    "invalid_plugin_name": "Config contains an invalid plugin identifier.",
    "invalid_plugin_value": "Every plugin value must be an explicit YAML boolean.",
    "invalid_registry": "Trusted route registry is invalid.",
    "empty_registry": "Trusted route registry must contain at least one route.",
    "invalid_route": "Trusted route binding is invalid.",
    "duplicate_route": "Trusted route registry contains a duplicate complete binding.",
    "cross_environment_reference": (
        "A sensitive route reference is reused across paper and live environments."
    ),
    "invalid_resolution_input": "Reference resolution requires a validated run config.",
    "plan_mismatch": "Resolved plan does not match the supplied run config.",
    "direct_plan_construction": "Resolved plans cannot be constructed through the public API.",
    "unregistered_route": "No trusted route is registered for this provider.",
    "route_environment_mismatch": (
        "No trusted route is registered for this provider and environment."
    ),
    "route_mismatch": "Run config references do not match the trusted route binding.",
    "plugin_resolution_deferred": "Enabled plugins cannot be resolved by this route registry.",
}


class RunConfigError(ValueError):
    """A sanitized configuration error with a stable, non-sensitive code."""

    def __init__(self, code: str) -> None:
        if code not in _MESSAGES:
            code = "invalid_schema"
        self.code = code
        super().__init__(_MESSAGES[code])
