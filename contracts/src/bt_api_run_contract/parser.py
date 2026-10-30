"""Strict parser for versioned pure-data run configuration schemas."""

from __future__ import annotations

import keyword
import re
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Dict, Tuple, Union

import yaml
from yaml.events import (
    AliasEvent,
    DocumentStartEvent,
    MappingEndEvent,
    MappingStartEvent,
    SequenceEndEvent,
    SequenceStartEvent,
)
from yaml.nodes import MappingNode, ScalarNode

from .errors import RunConfigError


SCHEMA_VERSION = 1
SCHEMA_V2_VERSION = 2
MAX_CONFIG_BYTES = 64 * 1024
MAX_YAML_EVENTS = 8192
MAX_COLLECTION_DEPTH = 32
MAX_STRATEGY_PARAMETERS = 64
MAX_STRATEGY_PARAMETER_DEPTH = 8
MAX_STRATEGY_PARAMETER_COLLECTION_ITEMS = 64
MAX_STRATEGY_PARAMETER_TOTAL_ITEMS = 512
MAX_STRATEGY_PARAMETER_NAME_LENGTH = 64
MAX_STRATEGY_PARAMETER_NAME_BYTES = 128
MAX_STRATEGY_PARAMETER_STRING_LENGTH = 256
MAX_STRATEGY_PARAMETER_STRING_BYTES = 512
MAX_V2_PLUGINS = 64
MIN_STRATEGY_INTEGER = -(2 ** 31)
MAX_STRATEGY_INTEGER = (2 ** 31) - 1
MAX_STRATEGY_FLOAT_ABS = 1.0e12
MAX_REQUESTED_CAPABILITIES = 4

_TOP_LEVEL_FIELDS = frozenset(
    {"schema_version", "mode", "environment", "references", "plugins"}
)
_REFERENCE_FIELDS = frozenset(
    {"strategy", "provider", "endpoint", "account", "credential", "scope"}
)
_REFERENCE_PATTERNS = {
    field: re.compile(r"^" + re.escape(field) + r"/[a-z][a-z0-9_-]{0,63}$")
    for field in _REFERENCE_FIELDS
}
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
_V2_REFERENCE_SET = frozenset(_V2_REFERENCE_FIELDS)
_V2_REFERENCE_PATTERNS = {
    field: re.compile(r"^" + re.escape(field) + r"/[a-z][a-z0-9_-]{0,63}$")
    for field in _V2_REFERENCE_FIELDS
}
_PLUGIN_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_REQUESTED_CAPABILITIES = frozenset(
    {
        "market.read",
        "account.read",
        "orders.paper.write",
        "orders.live.write",
    }
)
_WRITE_CAPABILITY_BY_MODE = {
    "paper": "orders.paper.write",
    "live": "orders.live.write",
}


class RunMode(str, Enum):
    REPLAY = "replay"
    SHADOW = "shadow"
    PAPER = "paper"
    LIVE = "live"


class Environment(str, Enum):
    PAPER = "paper"
    LIVE = "live"


@dataclass(frozen=True, repr=False)
class References:
    __slots__ = ("strategy", "provider", "endpoint", "account", "credential", "scope")

    strategy: str
    provider: str
    endpoint: str
    account: str
    credential: str
    scope: str

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("References cannot be subclassed")

    def __repr__(self) -> str:
        return "References(<redacted>)"

    def __post_init__(self) -> None:
        for name, pattern in _REFERENCE_PATTERNS.items():
            value = getattr(self, name)
            if type(value) is not str or pattern.fullmatch(value) is None:
                raise RunConfigError("invalid_reference")


@dataclass(frozen=True, repr=False)
class ValidatedRunConfig:
    """Immutable pure data; this object does not authorize or start a run."""

    __slots__ = ("schema_version", "mode", "environment", "references", "plugins")

    schema_version: int
    mode: RunMode
    environment: Environment
    references: References
    plugins: Tuple[Tuple[str, bool], ...]

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ValidatedRunConfig cannot be subclassed")

    def __repr__(self) -> str:
        return "ValidatedRunConfig(schema_version={!r}, mode={!r}, environment={!r})".format(
            self.schema_version, self.mode, self.environment
        )

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise RunConfigError("invalid_schema_version")
        if type(self.mode) is not RunMode or self.mode not in (RunMode.PAPER, RunMode.LIVE):
            raise RunConfigError("invalid_mode")
        if type(self.environment) is not Environment or self.environment.value != self.mode.value:
            raise RunConfigError("environment_mismatch")
        if type(self.references) is not References:
            raise RunConfigError("invalid_references")
        if type(self.plugins) is not tuple:
            raise RunConfigError("invalid_plugins")
        names = []
        for item in self.plugins:
            if type(item) is not tuple or len(item) != 2:
                raise RunConfigError("invalid_plugins")
            name, enabled = item
            if type(name) is not str or _PLUGIN_NAME.fullmatch(name) is None:
                raise RunConfigError("invalid_plugin_name")
            if type(enabled) is not bool:
                raise RunConfigError("invalid_plugin_value")
            names.append(name)
        if names != sorted(set(names)):
            raise RunConfigError("invalid_plugins")

    def plugin_enabled(self, name: str) -> bool:
        """Return a plugin switch value without exposing mutable state."""

        for plugin_name, enabled in self.plugins:
            if plugin_name == name:
                return enabled
        raise KeyError("plugin_not_declared")


@dataclass(frozen=True, repr=False)
class ReferencesV2:
    """Version 2's nine field-typed symbolic references; values stay redacted."""

    __slots__ = _V2_REFERENCE_FIELDS

    strategy: str
    provider: str
    endpoint: str
    account: str
    credential: str
    scope: str
    product: str
    data_source: str
    clock: str

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ReferencesV2 cannot be subclassed")

    def __repr__(self) -> str:
        return "ReferencesV2(<redacted>)"

    def __post_init__(self) -> None:
        for name, pattern in _V2_REFERENCE_PATTERNS.items():
            value = getattr(self, name)
            if type(value) is not str or pattern.fullmatch(value) is None:
                _raise("invalid_reference")


@dataclass(frozen=True, repr=False)
class StrategyParameterList:
    """Deeply immutable list value used by the bounded v2 parameter tree."""

    __slots__ = ("items",)

    items: Tuple[object, ...]

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("StrategyParameterList cannot be subclassed")

    def __repr__(self) -> str:
        return "StrategyParameterList(<redacted>)"

    def __post_init__(self) -> None:
        _validate_nested_parameter(self, 1, [0])

    def to_python(self) -> object:
        """Return a fresh ordinary list tree; no mutable storage is shared."""

        return _parameter_value_to_python(self)


@dataclass(frozen=True, repr=False)
class StrategyParameterMap:
    """Deeply immutable mapping value with sorted, string-keyed entries."""

    __slots__ = ("entries",)

    entries: Tuple[Tuple[str, object], ...]

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("StrategyParameterMap cannot be subclassed")

    def __repr__(self) -> str:
        return "StrategyParameterMap(<redacted>)"

    def __post_init__(self) -> None:
        _validate_nested_parameter(self, 1, [0])

    def to_python(self) -> object:
        """Return a fresh ordinary mapping tree; no mutable storage is shared."""

        return _parameter_value_to_python(self)


@dataclass(frozen=True, repr=False)
class ValidatedRunConfigV2:
    """Immutable schema-v2 input data; it requests capabilities but grants none."""

    __slots__ = (
        "schema_version",
        "mode",
        "environment",
        "references",
        "plugins",
        "strategy_parameters",
        "requested_capabilities",
    )

    schema_version: int
    mode: RunMode
    environment: Environment
    references: ReferencesV2
    plugins: Tuple[Tuple[str, bool], ...]
    strategy_parameters: Tuple[Tuple[str, object], ...]
    requested_capabilities: Tuple[str, ...]

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ValidatedRunConfigV2 cannot be subclassed")

    def __repr__(self) -> str:
        return "ValidatedRunConfigV2(schema_version=2, mode={!r}, environment={!r})".format(
            self.mode, self.environment
        )

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_V2_VERSION:
            _raise("invalid_schema_version")
        if type(self.mode) is not RunMode or self.mode not in (RunMode.PAPER, RunMode.LIVE):
            _raise("invalid_mode")
        if type(self.environment) is not Environment or self.environment.value != self.mode.value:
            _raise("environment_mismatch")
        if type(self.references) is not ReferencesV2:
            _raise("invalid_references")
        try:
            ReferencesV2(
                *(object.__getattribute__(self.references, name) for name in _V2_REFERENCE_FIELDS)
            )
        except (AttributeError, IndexError, TypeError):
            _raise("invalid_references")
        _validate_plugin_items(self.plugins)
        _validate_strategy_parameter_items(self.strategy_parameters)
        _validate_requested_capability_items(self.mode, self.requested_capabilities)

    def plugin_enabled(self, name: str) -> bool:
        """Return a plugin switch without loading or authorizing the plugin."""

        for plugin_name, enabled in self.plugins:
            if plugin_name == name:
                return enabled
        raise KeyError("plugin_not_declared")

    def strategy_parameter(self, name: str) -> object:
        """Return a declared immutable value without exposing it in repr output."""

        for parameter_name, value in self.strategy_parameters:
            if parameter_name == name:
                return value
        raise KeyError("strategy_parameter_not_declared")

    def strategy_parameters_dict(self) -> Dict[str, object]:
        """Return deep mutable copies for an explicit future adapter boundary."""

        _validate_strategy_parameter_items(self.strategy_parameters)
        return {
            name: _parameter_value_to_python(value)
            for name, value in self.strategy_parameters
        }


class _LoaderFault(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


_BOOL_TAG = "tag:yaml.org,2002:bool"
_BOOL_PATTERN = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
_MERGE_TAG = "tag:yaml.org,2002:merge"


class _StrictSafeLoader(yaml.SafeLoader):
    """SafeLoader without YAML 1.1's implicit yes/no/on/off booleans."""

    yaml_implicit_resolvers = {
        first: [(tag, pattern) for tag, pattern in resolvers if tag != _BOOL_TAG]
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }


for _first in "tTfF":
    _StrictSafeLoader.yaml_implicit_resolvers.setdefault(_first, []).append(
        (_BOOL_TAG, _BOOL_PATTERN)
    )


def _construct_strict_mapping(loader: _StrictSafeLoader, node: MappingNode, deep: bool = False):
    if not isinstance(node, MappingNode):
        raise _LoaderFault("invalid_yaml")

    result = {}
    for key_node, value_node in node.value:
        if not isinstance(key_node, ScalarNode) or key_node.tag == _MERGE_TAG:
            raise _LoaderFault("invalid_yaml")
        key = loader.construct_object(key_node, deep=deep)
        if type(key) is not str:
            raise _LoaderFault("invalid_yaml")
        if key in result:
            raise _LoaderFault("duplicate_key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictSafeLoader.construct_mapping = _construct_strict_mapping


def _raise(code: str) -> None:
    raise RunConfigError(code) from None


def _input_text(source: Union[str, bytes]) -> str:
    if type(source) is bytes:
        if len(source) > MAX_CONFIG_BYTES:
            _raise("input_too_large")
        try:
            text = source.decode("utf-8", "strict")
        except UnicodeDecodeError:
            text = None
        if text is None:
            _raise("invalid_utf8")
    elif type(source) is str:
        # A Unicode string cannot encode to fewer UTF-8 bytes than characters.
        # Reject the obvious oversized case before allocating another copy.
        if len(source) > MAX_CONFIG_BYTES:
            _raise("input_too_large")
        try:
            encoded = source.encode("utf-8", "strict")
        except UnicodeEncodeError:
            encoded = None
        if encoded is None:
            _raise("invalid_utf8")
        if len(encoded) > MAX_CONFIG_BYTES:
            _raise("input_too_large")
        text = source
    else:
        _raise("invalid_input_type")

    # Accept the UTF-8 signature only at the beginning of an otherwise valid
    # document; it is not part of the YAML scalar data.
    if text.startswith("\ufeff"):
        text = text[1:]
    return text


def _scan_yaml_features(text: str) -> None:
    document_count = 0
    collection_depth = 0
    event_count = 0
    fault_code = None
    try:
        for event in yaml.parse(text, Loader=_StrictSafeLoader):
            event_count += 1
            if event_count > MAX_YAML_EVENTS:
                raise _LoaderFault("document_too_complex")
            if (
                isinstance(event, AliasEvent)
                or getattr(event, "anchor", None) is not None
                or getattr(event, "tag", None) is not None
            ):
                raise _LoaderFault("invalid_yaml")
            if isinstance(event, DocumentStartEvent):
                document_count += 1
                if document_count > 1:
                    raise _LoaderFault("multiple_documents")
            if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
                collection_depth += 1
                if collection_depth > MAX_COLLECTION_DEPTH:
                    raise _LoaderFault("document_too_complex")
            elif isinstance(event, (MappingEndEvent, SequenceEndEvent)):
                collection_depth -= 1
    except _LoaderFault as fault:
        fault_code = fault.code
    except Exception:
        # PyYAML exceptions may include source snippets. Never preserve them in
        # a public diagnostic or exception chain.
        fault_code = "invalid_yaml"

    if fault_code is not None:
        # Do not retain the parser exception in an accessible __context__.
        raise _LoaderFault(fault_code)

    if document_count != 1:
        raise _LoaderFault("invalid_yaml")


def _load_yaml(text: str) -> object:
    loader = _StrictSafeLoader(text)
    result = None
    fault_code = None
    try:
        result = loader.get_single_data()
    except _LoaderFault as fault:
        fault_code = fault.code
    except Exception:
        fault_code = "invalid_yaml"
    finally:
        loader.dispose()
    if fault_code is not None:
        raise _LoaderFault(fault_code)
    return result


def _mapping_with_fields(value: object, fields: frozenset, required: frozenset) -> Dict[str, object]:
    if type(value) is not dict:
        _raise("invalid_schema")
    keys = set(value)
    if not keys.issubset(fields):
        _raise("unknown_field")
    if not required.issubset(keys):
        _raise("missing_field")
    return value


def _valid_bounded_text(
    value: object,
    max_length: int,
    max_bytes: int,
    allow_empty: bool = False,
) -> bool:
    if type(value) is not str or (not value and not allow_empty) or len(value) > max_length:
        return False
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return False
    return len(encoded) <= max_bytes and not any(
        ord(char) < 0x20 or ord(char) == 0x7F for char in value
    )


def _validate_strategy_parameter_name(name: object) -> None:
    if (
        not _valid_bounded_text(
            name,
            MAX_STRATEGY_PARAMETER_NAME_LENGTH,
            MAX_STRATEGY_PARAMETER_NAME_BYTES,
        )
        or not name.isidentifier()
        or keyword.iskeyword(name)
    ):
        _raise("invalid_strategy_parameter_name")


def _consume_parameter_budget(depth: int, budget: list) -> None:
    if depth > MAX_STRATEGY_PARAMETER_DEPTH:
        _raise("invalid_strategy_parameter_value")
    budget[0] += 1
    if budget[0] > MAX_STRATEGY_PARAMETER_TOTAL_ITEMS:
        _raise("invalid_strategy_parameter_value")


def _validate_scalar_parameter(value: object) -> bool:
    if value is None or type(value) is bool:
        return True
    if type(value) is int:
        return MIN_STRATEGY_INTEGER <= value <= MAX_STRATEGY_INTEGER
    if type(value) is float:
        return isfinite(value) and abs(value) <= MAX_STRATEGY_FLOAT_ABS
    if type(value) is str:
        return _valid_bounded_text(
            value,
            MAX_STRATEGY_PARAMETER_STRING_LENGTH,
            MAX_STRATEGY_PARAMETER_STRING_BYTES,
            allow_empty=True,
        )
    return False


def _validate_nested_parameter(value: object, depth: int, budget: list) -> None:
    _consume_parameter_budget(depth, budget)
    if _validate_scalar_parameter(value):
        return
    if type(value) is StrategyParameterList:
        try:
            items = object.__getattribute__(value, "items")
        except AttributeError:
            _raise("invalid_strategy_parameter_value")
        if type(items) is not tuple or len(items) > MAX_STRATEGY_PARAMETER_COLLECTION_ITEMS:
            _raise("invalid_strategy_parameter_value")
        for item in items:
            _validate_nested_parameter(item, depth + 1, budget)
        return
    if type(value) is StrategyParameterMap:
        try:
            entries = object.__getattribute__(value, "entries")
        except AttributeError:
            _raise("invalid_strategy_parameter_value")
        if type(entries) is not tuple or len(entries) > MAX_STRATEGY_PARAMETER_COLLECTION_ITEMS:
            _raise("invalid_strategy_parameter_value")
        keys = []
        for entry in entries:
            if type(entry) is not tuple or len(entry) != 2:
                _raise("invalid_strategy_parameter_value")
            key, nested_value = entry
            if not _valid_bounded_text(
                key,
                MAX_STRATEGY_PARAMETER_STRING_LENGTH,
                MAX_STRATEGY_PARAMETER_STRING_BYTES,
                allow_empty=True,
            ):
                _raise("invalid_strategy_parameter_value")
            keys.append(key)
            _validate_nested_parameter(nested_value, depth + 1, budget)
        if keys != sorted(set(keys)):
            _raise("invalid_strategy_parameter_value")
        return
    _raise("invalid_strategy_parameter_value")


def _normalize_nested_parameter(value: object, depth: int, budget: list) -> object:
    _consume_parameter_budget(depth, budget)
    if _validate_scalar_parameter(value):
        return value
    if type(value) is list:
        if len(value) > MAX_STRATEGY_PARAMETER_COLLECTION_ITEMS:
            _raise("invalid_strategy_parameter_value")
        items = tuple(
            _normalize_nested_parameter(item, depth + 1, budget) for item in value
        )
        return StrategyParameterList(items)
    if type(value) is dict:
        if len(value) > MAX_STRATEGY_PARAMETER_COLLECTION_ITEMS:
            _raise("invalid_strategy_parameter_value")
        entries = []
        for key, nested_value in value.items():
            if not _valid_bounded_text(
                key,
                MAX_STRATEGY_PARAMETER_STRING_LENGTH,
                MAX_STRATEGY_PARAMETER_STRING_BYTES,
                allow_empty=True,
            ):
                _raise("invalid_strategy_parameter_value")
            entries.append(
                (key, _normalize_nested_parameter(nested_value, depth + 1, budget))
            )
        return StrategyParameterMap(tuple(sorted(entries)))
    _raise("invalid_strategy_parameter_value")


def _parameter_value_to_python(value: object) -> object:
    _validate_nested_parameter(value, 1, [0])
    if type(value) is StrategyParameterList:
        return [_parameter_value_to_python(item) for item in value.items]
    if type(value) is StrategyParameterMap:
        return {
            key: _parameter_value_to_python(item) for key, item in value.entries
        }
    return value


def _validate_strategy_parameter_items(items: object) -> None:
    if type(items) is not tuple:
        _raise("invalid_strategy_parameters")
    if len(items) > MAX_STRATEGY_PARAMETERS:
        _raise("strategy_parameters_too_large")
    names = []
    budget = [0]
    for item in items:
        if type(item) is not tuple or len(item) != 2:
            _raise("invalid_strategy_parameters")
        name, value = item
        _validate_strategy_parameter_name(name)
        _validate_nested_parameter(value, 1, budget)
        names.append(name)
    if names != sorted(set(names)):
        _raise("invalid_strategy_parameters")


def _parse_strategy_parameters(value: object) -> Tuple[Tuple[str, object], ...]:
    if type(value) is not dict:
        _raise("invalid_strategy_parameters")
    if len(value) > MAX_STRATEGY_PARAMETERS:
        _raise("strategy_parameters_too_large")
    budget = [0]
    parameters = []
    for name, parameter_value in value.items():
        _validate_strategy_parameter_name(name)
        parameters.append(
            (name, _normalize_nested_parameter(parameter_value, 1, budget))
        )
    return tuple(sorted(parameters))


def _validate_plugin_items(items: object) -> None:
    if type(items) is not tuple or len(items) > MAX_V2_PLUGINS:
        _raise("invalid_plugins")
    names = []
    for item in items:
        if type(item) is not tuple or len(item) != 2:
            _raise("invalid_plugins")
        name, enabled = item
        if type(name) is not str or _PLUGIN_NAME.fullmatch(name) is None:
            _raise("invalid_plugin_name")
        if type(enabled) is not bool:
            _raise("invalid_plugin_value")
        names.append(name)
    if names != sorted(set(names)):
        _raise("invalid_plugins")


def _parse_plugins(value: object) -> Tuple[Tuple[str, bool], ...]:
    if type(value) is not dict or len(value) > MAX_V2_PLUGINS:
        _raise("invalid_plugins")
    plugins = []
    for name, enabled in value.items():
        if type(name) is not str or _PLUGIN_NAME.fullmatch(name) is None:
            _raise("invalid_plugin_name")
        if type(enabled) is not bool:
            _raise("invalid_plugin_value")
        plugins.append((name, enabled))
    return tuple(sorted(plugins))


def _validate_requested_capability_items(mode: object, capabilities: object) -> None:
    if type(capabilities) is not tuple:
        _raise("invalid_capabilities")
    if len(capabilities) > MAX_REQUESTED_CAPABILITIES:
        _raise("invalid_capabilities")
    for capability in capabilities:
        if type(capability) is not str:
            _raise("invalid_capabilities")
        if capability not in _REQUESTED_CAPABILITIES:
            _raise("unknown_capability")
    if capabilities != tuple(sorted(set(capabilities))):
        if len(set(capabilities)) != len(capabilities):
            _raise("duplicate_capability")
        _raise("invalid_capabilities")
    if type(mode) is RunMode and mode in (RunMode.PAPER, RunMode.LIVE):
        expected_write = _WRITE_CAPABILITY_BY_MODE[mode.value]
        if any(
            capability in ("orders.paper.write", "orders.live.write")
            and capability != expected_write
            for capability in capabilities
        ):
            _raise("capability_mode_mismatch")


def _parse_requested_capabilities(mode: RunMode, value: object) -> Tuple[str, ...]:
    if type(value) is not list:
        _raise("invalid_capabilities")
    if len(value) > MAX_REQUESTED_CAPABILITIES:
        _raise("invalid_capabilities")
    for capability in value:
        if type(capability) is not str:
            _raise("invalid_capabilities")
        if capability not in _REQUESTED_CAPABILITIES:
            _raise("unknown_capability")
    if len(set(value)) != len(value):
        _raise("duplicate_capability")
    capabilities = tuple(sorted(value))
    _validate_requested_capability_items(mode, capabilities)
    return capabilities


def _parse_v2(raw: Dict[str, object]) -> ValidatedRunConfigV2:
    if not set(raw).issubset(
        {
            "schema_version",
            "mode",
            "environment",
            "references",
            "plugins",
            "strategy_parameters",
            "requested_capabilities",
        }
    ):
        _raise("unknown_field")
    if "mode" not in raw:
        _raise("missing_field")
    mode_value = raw["mode"]
    if type(mode_value) is not str:
        _raise("invalid_mode")
    try:
        mode = RunMode(mode_value)
    except ValueError:
        mode = None
    if mode is None:
        _raise("invalid_mode")
    if mode in (RunMode.REPLAY, RunMode.SHADOW):
        _raise("mode_deferred")

    fields = frozenset(
        {
            "schema_version",
            "mode",
            "environment",
            "references",
            "plugins",
            "strategy_parameters",
            "requested_capabilities",
        }
    )
    required = fields
    _mapping_with_fields(raw, fields, required)

    environment_value = raw["environment"]
    if type(environment_value) is not str:
        _raise("invalid_environment")
    try:
        environment = Environment(environment_value)
    except ValueError:
        environment = None
    if environment is None:
        _raise("invalid_environment")
    if environment.value != mode.value:
        _raise("environment_mismatch")

    references_map = _mapping_with_fields(
        raw["references"],
        _V2_REFERENCE_SET,
        _V2_REFERENCE_SET,
    )
    for field, pattern in _V2_REFERENCE_PATTERNS.items():
        value = references_map[field]
        if type(value) is not str or pattern.fullmatch(value) is None:
            _raise("invalid_reference")
    references = ReferencesV2(
        *(references_map[field] for field in _V2_REFERENCE_FIELDS)
    )
    plugins = _parse_plugins(raw["plugins"])
    strategy_parameters = _parse_strategy_parameters(raw["strategy_parameters"])
    requested_capabilities = _parse_requested_capabilities(
        mode, raw["requested_capabilities"]
    )
    return ValidatedRunConfigV2(
        schema_version=SCHEMA_V2_VERSION,
        mode=mode,
        environment=environment,
        references=references,
        plugins=plugins,
        strategy_parameters=strategy_parameters,
        requested_capabilities=requested_capabilities,
    )


def parse_config(source: Union[str, bytes]) -> Union[ValidatedRunConfig, ValidatedRunConfigV2]:
    """Parse caller-provided YAML into immutable data without I/O or imports.

    This validates structure and symbolic-reference spelling only. It does not
    resolve references, load plugins or strategies, grant permissions, or run
    a provider. Schema v2 returns its own DTO; its resolver integration is
    explicitly deferred and does not produce the version-1 plan tuple.
    """

    text = _input_text(source)
    fault_code = None
    try:
        _scan_yaml_features(text)
        raw = _load_yaml(text)
    except _LoaderFault as fault:
        fault_code = fault.code
    if fault_code is not None:
        _raise(fault_code)

    if type(raw) is not dict:
        _raise("invalid_schema")

    # Dispatch by version before schema-specific shape checks so old schemas
    # fail explicitly and can never be silently mapped into another version.
    if "schema_version" not in raw:
        _raise("missing_field")
    version = raw["schema_version"]
    if type(version) is not int:
        _raise("invalid_schema_version")
    if version == SCHEMA_V2_VERSION:
        return _parse_v2(raw)
    if version != SCHEMA_VERSION:
        _raise("unsupported_schema_version")

    if not set(raw).issubset(_TOP_LEVEL_FIELDS):
        _raise("unknown_field")
    if "mode" not in raw:
        _raise("missing_field")
    mode_value = raw["mode"]
    if type(mode_value) is not str:
        _raise("invalid_mode")
    try:
        mode = RunMode(mode_value)
    except ValueError:
        mode = None
    if mode is None:
        _raise("invalid_mode")
    if mode in (RunMode.REPLAY, RunMode.SHADOW):
        _raise("mode_deferred")

    required = frozenset({"schema_version", "mode", "environment", "references", "plugins"})
    _mapping_with_fields(raw, _TOP_LEVEL_FIELDS, required)

    environment_value = raw["environment"]
    if type(environment_value) is not str:
        _raise("invalid_environment")
    try:
        environment = Environment(environment_value)
    except ValueError:
        environment = None
    if environment is None:
        _raise("invalid_environment")
    if environment.value != mode.value:
        _raise("environment_mismatch")

    raw_references = raw["references"]
    references_map = _mapping_with_fields(
        raw_references,
        _REFERENCE_FIELDS,
        _REFERENCE_FIELDS,
    )
    for field, pattern in _REFERENCE_PATTERNS.items():
        value = references_map[field]
        if type(value) is not str or pattern.fullmatch(value) is None:
            _raise("invalid_reference")
    references = References(
        strategy=references_map["strategy"],
        provider=references_map["provider"],
        endpoint=references_map["endpoint"],
        account=references_map["account"],
        credential=references_map["credential"],
        scope=references_map["scope"],
    )

    raw_plugins = raw["plugins"]
    if type(raw_plugins) is not dict:
        _raise("invalid_plugins")
    plugin_switches = []
    for name, enabled in raw_plugins.items():
        if type(name) is not str or _PLUGIN_NAME.fullmatch(name) is None:
            _raise("invalid_plugin_name")
        if type(enabled) is not bool:
            _raise("invalid_plugin_value")
        plugin_switches.append((name, enabled))

    return ValidatedRunConfig(
        schema_version=SCHEMA_VERSION,
        mode=mode,
        environment=environment,
        references=references,
        plugins=tuple(sorted(plugin_switches)),
    )
