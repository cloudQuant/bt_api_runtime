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
    "invalid_mode": "Config mode is invalid.",
    "mode_deferred": "This mode is recognized but deferred and cannot produce a run config.",
    "invalid_environment": "Config environment is invalid.",
    "environment_mismatch": "Config mode and environment do not match.",
    "invalid_references": "Config references must be a mapping of symbolic identifiers.",
    "invalid_reference": "Config contains an invalid symbolic reference.",
    "invalid_plugins": "Config plugins must be a mapping of symbolic identifiers to booleans.",
    "invalid_plugin_name": "Config contains an invalid plugin identifier.",
    "invalid_plugin_value": "Every plugin value must be an explicit YAML boolean.",
}


class RunConfigError(ValueError):
    """A sanitized configuration error with a stable, non-sensitive code."""

    def __init__(self, code: str) -> None:
        if code not in _MESSAGES:
            code = "invalid_schema"
        self.code = code
        super().__init__(_MESSAGES[code])
