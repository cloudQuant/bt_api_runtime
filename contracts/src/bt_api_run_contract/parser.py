"""Strict parser for the version 1 pure-data run configuration schema."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
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
MAX_CONFIG_BYTES = 64 * 1024
MAX_YAML_EVENTS = 8192
MAX_COLLECTION_DEPTH = 32

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
_PLUGIN_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


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


def parse_config(source: Union[str, bytes]) -> ValidatedRunConfig:
    """Parse caller-provided YAML into immutable data without I/O or imports.

    This validates structure and symbolic-reference spelling only. It does not
    resolve references, load plugins or strategies, grant permissions, or run
    a provider.
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

    # Check the version before the rest of the shape so legacy schemas fail
    # explicitly and can never be silently mapped into schema v1.
    if "schema_version" not in raw:
        _raise("missing_field")
    version = raw["schema_version"]
    if type(version) is not int:
        _raise("invalid_schema_version")
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
