import builtins
import os
import socket
import subprocess
import unittest
import urllib.request
from dataclasses import FrozenInstanceError

from bt_api_run_contract import (
    Environment,
    MAX_CONFIG_BYTES,
    MAX_STRATEGY_PARAMETER_COLLECTION_ITEMS,
    MAX_STRATEGY_PARAMETER_DEPTH,
    MAX_STRATEGY_PARAMETER_NAME_LENGTH,
    MAX_STRATEGY_PARAMETERS,
    MAX_STRATEGY_PARAMETER_TOTAL_ITEMS,
    References,
    ReferencesV2,
    RunConfigError,
    RunMode,
    SCHEMA_VERSION,
    SCHEMA_V2_VERSION,
    StrategyParameterList,
    StrategyParameterMap,
    ValidatedRunConfig,
    ValidatedRunConfigV2,
    parse_config,
)


VALID_PAPER = """\
schema_version: 1
mode: paper
environment: paper
references:
  strategy: strategy/intraday_v1
  provider: provider/binance_usdm
  endpoint: endpoint/binance_paper
  account: account/paper_primary
  credential: credential/paper_trading
  scope: scope/usdm_paper_orders
plugins:
  trade_logger: true
  metrics: false
"""


def _config(mode="paper", environment="paper", plugins="  metrics: false\n"):
    return (
        "schema_version: 1\n"
        "mode: {}\n".format(mode)
        + "environment: {}\n".format(environment)
        + "references:\n"
        "  strategy: strategy/intraday_v1\n"
        "  provider: provider/binance_usdm\n"
        "  endpoint: endpoint/binance_paper\n"
        "  account: account/paper_primary\n"
        "  credential: credential/paper_trading\n"
        "  scope: scope/usdm_paper_orders\n"
        "plugins:\n"
        + plugins
    )


VALID_PAPER_V2 = """\
schema_version: 2
mode: paper
environment: paper
references:
  strategy: strategy/intraday_v2
  provider: provider/binance_usdm
  endpoint: endpoint/binance_paper
  account: account/paper_primary
  credential: credential/paper_trading
  scope: scope/usdm_paper_orders
  product: product/binance_usdm
  data_source: data_source/market_feed
  clock: clock/exchange_utc
plugins:
  gateway: true
  risk: false
  monitor: true
  execution: false
  runtime: true
strategy_parameters:
  window: 20
  threshold: 0.5
  use_filter: true
requested_capabilities:
  - market.read
  - account.read
  - orders.paper.write
"""


def _v2_config(
    mode="paper",
    environment=None,
    parameters="  window: 20\n",
    capabilities="  - market.read\n",
):
    if environment is None:
        environment = mode
    return (
        "schema_version: 2\n"
        "mode: {}\n".format(mode)
        + "environment: {}\n".format(environment)
        + "references:\n"
        "  strategy: strategy/intraday_v2\n"
        "  provider: provider/binance_usdm\n"
        "  endpoint: endpoint/binance_paper\n"
        "  account: account/paper_primary\n"
        "  credential: credential/paper_trading\n"
        "  scope: scope/usdm_paper_orders\n"
        "  product: product/binance_usdm\n"
        "  data_source: data_source/market_feed\n"
        "  clock: clock/exchange_utc\n"
        "plugins:\n"
        "  gateway: true\n"
        "  risk: false\n"
        "  monitor: true\n"
        "  execution: false\n"
        "  runtime: true\n"
        "strategy_parameters:\n"
        + parameters
        + (
            "requested_capabilities: []\n"
            if capabilities == ""
            else "requested_capabilities:\n" + capabilities
        )
    )


class ParseConfigTests(unittest.TestCase):
    def assert_code(self, code, source):
        with self.assertRaises(RunConfigError) as caught:
            parse_config(source)
        self.assertEqual(caught.exception.code, code)

    def test_paper_and_live_return_immutable_reference_only_data(self):
        paper = parse_config(VALID_PAPER.encode("utf-8"))
        live = parse_config(_config("live", "live"))

        self.assertEqual(paper.mode, RunMode.PAPER)
        self.assertEqual(paper.environment, Environment.PAPER)
        self.assertEqual(paper.references.credential, "credential/paper_trading")
        self.assertTrue(paper.plugin_enabled("trade_logger"))
        self.assertFalse(paper.plugin_enabled("metrics"))
        with self.assertRaises(KeyError):
            paper.plugin_enabled("unknown")
        self.assertEqual(live.mode, RunMode.LIVE)
        self.assertEqual(live.environment, Environment.LIVE)
        self.assertEqual(paper.plugins, (("metrics", False), ("trade_logger", True)))

        with self.assertRaises(FrozenInstanceError):
            paper.mode = RunMode.LIVE
        with self.assertRaises(FrozenInstanceError):
            paper.references.strategy = "strategy/other"
        with self.assertRaises(TypeError):
            paper.plugins[0] = ("metrics", True)
        with self.assertRaises(TypeError):
            vars(paper)
        with self.assertRaises(TypeError):
            vars(paper.references)

    def test_schema_version_dispatch_preserves_v1_and_builds_independent_v2_dto(self):
        self.assertEqual(SCHEMA_VERSION, 1)
        self.assertEqual(SCHEMA_V2_VERSION, 2)

        v1 = parse_config(VALID_PAPER)
        self.assertIs(type(v1), ValidatedRunConfig)
        self.assertEqual(v1.plugins, (("metrics", False), ("trade_logger", True)))

        paper = parse_config(VALID_PAPER_V2)
        live_source = _v2_config(
            mode="live",
            capabilities="  - account.read\n  - market.read\n  - orders.live.write\n",
        )
        live = parse_config(live_source)

        self.assertIs(type(paper), ValidatedRunConfigV2)
        self.assertIs(type(paper.references), ReferencesV2)
        self.assertEqual(paper.schema_version, 2)
        self.assertEqual(paper.mode, RunMode.PAPER)
        self.assertEqual(paper.environment, Environment.PAPER)
        self.assertEqual(paper.references.strategy, "strategy/intraday_v2")
        self.assertEqual(paper.references.product, "product/binance_usdm")
        self.assertEqual(paper.references.data_source, "data_source/market_feed")
        self.assertEqual(paper.references.clock, "clock/exchange_utc")
        self.assertEqual(
            paper.plugins,
            (
                ("execution", False),
                ("gateway", True),
                ("monitor", True),
                ("risk", False),
                ("runtime", True),
            ),
        )
        self.assertTrue(paper.plugin_enabled("gateway"))
        self.assertFalse(paper.plugin_enabled("risk"))
        with self.assertRaises(KeyError):
            paper.plugin_enabled("missing")
        self.assertEqual(
            paper.strategy_parameters,
            (("threshold", 0.5), ("use_filter", True), ("window", 20)),
        )
        self.assertEqual(paper.strategy_parameter("window"), 20)
        with self.assertRaises(KeyError):
            paper.strategy_parameter("missing")
        self.assertEqual(
            paper.requested_capabilities,
            ("account.read", "market.read", "orders.paper.write"),
        )
        self.assertEqual(live.mode, RunMode.LIVE)
        self.assertEqual(live.environment, Environment.LIVE)
        self.assertIn("orders.live.write", live.requested_capabilities)

        with self.assertRaises(FrozenInstanceError):
            paper.mode = RunMode.LIVE
        with self.assertRaises(FrozenInstanceError):
            paper.references.clock = "clock/other"
        with self.assertRaises(TypeError):
            paper.strategy_parameters[0] = ("window", 5)
        with self.assertRaises(TypeError):
            vars(paper)
        with self.assertRaises(TypeError):
            vars(paper.references)

    def test_v2_replay_and_shadow_are_recognized_but_parse_is_deferred(self):
        for mode in ("replay", "shadow"):
            with self.subTest(mode=mode):
                self.assert_code("mode_deferred", _v2_config(mode=mode))

    def test_v2_mode_environment_and_write_capability_must_match(self):
        self.assert_code(
            "environment_mismatch",
            _v2_config(mode="paper", environment="live"),
        )
        self.assert_code(
            "environment_mismatch",
            _v2_config(mode="live", environment="paper"),
        )
        self.assert_code(
            "capability_mode_mismatch",
            _v2_config(capabilities="  - orders.live.write\n"),
        )
        self.assert_code(
            "capability_mode_mismatch",
            _v2_config(
                mode="live",
                capabilities="  - orders.paper.write\n",
            ),
        )
        self.assert_code(
            "capability_mode_mismatch",
            _v2_config(
                capabilities="  - orders.paper.write\n  - orders.live.write\n"
            ),
        )

    def test_v2_references_are_explicitly_typed_and_complete(self):
        for field, unsafe in (
            ("strategy", "provider/wrong_kind"),
            ("provider", "provider/actual-secret-value!"),
            ("endpoint", "https://example.invalid/api"),
            ("account", "account/BadName"),
            ("credential", "module.Class"),
            ("scope", "scope/has space"),
            ("product", "binance_usdm"),
            ("data_source", "data-source/market"),
            ("clock", "clock/"),
        ):
            with self.subTest(field=field):
                original_value = {
                    "strategy": "strategy/intraday_v2",
                    "provider": "provider/binance_usdm",
                    "endpoint": "endpoint/binance_paper",
                    "account": "account/paper_primary",
                    "credential": "credential/paper_trading",
                    "scope": "scope/usdm_paper_orders",
                    "product": "product/binance_usdm",
                    "data_source": "data_source/market_feed",
                    "clock": "clock/exchange_utc",
                }[field]
                source = VALID_PAPER_V2.replace(
                    "  {}: {}\n".format(field, original_value),
                    "  {}: {}\n".format(field, unsafe),
                )
                self.assert_code("invalid_reference", source)

        self.assert_code(
            "missing_field",
            VALID_PAPER_V2.replace("  clock: clock/exchange_utc\n", ""),
        )

    def test_v2_strategy_parameters_have_bounded_names_types_and_values(self):
        self.assertIsInstance(
            parse_config(_v2_config(parameters="  lower: -2147483648\n")),
            ValidatedRunConfigV2,
        )
        self.assertIsInstance(
            parse_config(
                _v2_config(parameters="  {}: 1\n".format("A" * MAX_STRATEGY_PARAMETER_NAME_LENGTH))
            ),
            ValidatedRunConfigV2,
        )
        self.assertIsInstance(
            parse_config(_v2_config(parameters="  upper: 2147483647\n")),
            ValidatedRunConfigV2,
        )
        self.assertIsInstance(
            parse_config(_v2_config(parameters="  threshold: 1000000000000.0\n")),
            ValidatedRunConfigV2,
        )
        self.assertIsInstance(
            parse_config(_v2_config(parameters='  text: "{}"\n'.format("é" * 256))),
            ValidatedRunConfigV2,
        )
        self.assertIsInstance(
            parse_config(_v2_config(parameters='  text: "{}"\n'.format("€" * 170))),
            ValidatedRunConfigV2,
        )
        self.assertIsNone(
            parse_config(_v2_config(parameters="  optional: null\n")).strategy_parameter(
                "optional"
            )
        )

        for parameters, code in (
            ("  bad-name: 1\n", "invalid_strategy_parameter_name"),
            (
                "  {}: 1\n".format("A" * (MAX_STRATEGY_PARAMETER_NAME_LENGTH + 1)),
                "invalid_strategy_parameter_name",
            ),
            ("  low: -2147483649\n", "invalid_strategy_parameter_value"),
            ("  high: 2147483648\n", "invalid_strategy_parameter_value"),
            ("  high: 1000000000001.0\n", "invalid_strategy_parameter_value"),
            ("  not_finite: .nan\n", "invalid_strategy_parameter_value"),
            ("  too_long: \"{}\"\n".format("x" * 257), "invalid_strategy_parameter_value"),
            ("  too_many_bytes: \"{}\"\n".format("€" * 171), "invalid_strategy_parameter_value"),
            ("  not_plain_data: 2026-10-30\n", "invalid_strategy_parameter_value"),
        ):
            with self.subTest(code=code, parameters=parameters[:24]):
                self.assert_code(code, _v2_config(parameters=parameters))

        maximum = "".join(
            "  p{}: 1\n".format(index) for index in range(MAX_STRATEGY_PARAMETERS)
        )
        too_many = maximum + "  p64: 1\n"
        self.assertEqual(
            len(parse_config(_v2_config(parameters=maximum)).strategy_parameters),
            MAX_STRATEGY_PARAMETERS,
        )
        self.assert_code("strategy_parameters_too_large", _v2_config(parameters=too_many))

    def test_v2_nested_parameters_are_bounded_and_deeply_immutable(self):
        source = _v2_config(
            parameters=(
                "  Symbols: [BTCUSDT, ETHUSDT]\n"
                "  sizing:\n"
                "    max_positions: 5\n"
                "    targets:\n"
                "      - market: BTCUSDT\n"
                "        weight: 0.5\n"
                "      - market: ETHUSDT\n"
                "        weight: null\n"
            )
        )
        value = parse_config(source)
        symbols = value.strategy_parameter("Symbols")
        sizing = value.strategy_parameter("sizing")
        self.assertIs(type(symbols), StrategyParameterList)
        self.assertEqual(symbols.items, ("BTCUSDT", "ETHUSDT"))
        self.assertIs(type(sizing), StrategyParameterMap)
        self.assertEqual(
            tuple(key for key, _nested in sizing.entries),
            ("max_positions", "targets"),
        )
        copied = value.strategy_parameters_dict()
        self.assertEqual(
            copied["Symbols"],
            ["BTCUSDT", "ETHUSDT"],
        )
        copied["Symbols"].append("MUTATED")
        copied["sizing"]["targets"][0]["market"] = "MUTATED"
        self.assertEqual(
            value.strategy_parameters_dict()["Symbols"],
            ["BTCUSDT", "ETHUSDT"],
        )
        self.assertEqual(
            value.strategy_parameters_dict()["sizing"]["targets"][0]["market"],
            "BTCUSDT",
        )
        self.assertEqual(
            symbols.to_python(),
            ["BTCUSDT", "ETHUSDT"],
        )
        with self.assertRaises(FrozenInstanceError):
            symbols.items = ("OTHER",)
        with self.assertRaises(TypeError):
            symbols.items[0] = "OTHER"

        maximum_items = ", ".join("1" for _ in range(MAX_STRATEGY_PARAMETER_COLLECTION_ITEMS))
        self.assertIsInstance(
            parse_config(
                _v2_config(parameters="  values: [{}]\n".format(maximum_items))
            ),
            ValidatedRunConfigV2,
        )
        too_many_items = maximum_items + ", 1"
        self.assert_code(
            "invalid_strategy_parameter_value",
            _v2_config(parameters="  values: [{}]\n".format(too_many_items)),
        )

        allowed_depth = "[" * (MAX_STRATEGY_PARAMETER_DEPTH - 1) + "1" + "]" * (
            MAX_STRATEGY_PARAMETER_DEPTH - 1
        )
        denied_depth = "[" * MAX_STRATEGY_PARAMETER_DEPTH + "1" + "]" * (
            MAX_STRATEGY_PARAMETER_DEPTH
        )
        self.assertIsInstance(
            parse_config(_v2_config(parameters="  nested: {}\n".format(allowed_depth))),
            ValidatedRunConfigV2,
        )
        self.assert_code(
            "invalid_strategy_parameter_value",
            _v2_config(parameters="  nested: {}\n".format(denied_depth)),
        )

        at_total_limit = "".join(
            "  p{}: [{}]\n".format(index, ", ".join("1" for _ in range(7)))
            for index in range(MAX_STRATEGY_PARAMETERS)
        )
        over_total_limit = at_total_limit.replace(
            "  p0: [1, 1, 1, 1, 1, 1, 1]\n",
            "  p0: [1, 1, 1, 1, 1, 1, 1, 1]\n",
        )
        self.assertEqual(MAX_STRATEGY_PARAMETER_TOTAL_ITEMS, 512)
        self.assertIsInstance(
            parse_config(_v2_config(parameters=at_total_limit)),
            ValidatedRunConfigV2,
        )
        self.assert_code(
            "invalid_strategy_parameter_value",
            _v2_config(parameters=over_total_limit),
        )

    def test_v2_requested_capabilities_are_bounded_known_and_deduplicated(self):
        self.assertEqual(
            parse_config(_v2_config(capabilities="")).requested_capabilities,
            (),
        )
        self.assert_code(
            "unknown_capability",
            _v2_config(capabilities="  - orders.write\n"),
        )
        self.assert_code(
            "duplicate_capability",
            _v2_config(capabilities="  - market.read\n  - market.read\n"),
        )
        self.assert_code(
            "invalid_capabilities",
            _v2_config(capabilities="  - true\n"),
        )
        self.assert_code(
            "invalid_capabilities",
            _v2_config(capabilities="  - market.read\n  - account.read\n"
                                   "  - orders.paper.write\n  - orders.live.write\n"
                                   "  - market.read\n"),
        )

    def test_v2_rejects_v1_and_legacy_fields_without_implicit_migration(self):
        self.assert_code("unknown_field", VALID_PAPER_V2 + "preset: paper\n")
        self.assert_code("unknown_field", VALID_PAPER_V2 + "local_simulation: true\n")
        self.assert_code(
            "unsupported_schema_version",
            VALID_PAPER_V2.replace("schema_version: 2", "schema_version: 4"),
        )

    def test_v2_plugin_switches_are_required_explicit_boolean_data(self):
        self.assert_code(
            "missing_field",
            VALID_PAPER_V2.replace(
                "plugins:\n"
                "  gateway: true\n"
                "  risk: false\n"
                "  monitor: true\n"
                "  execution: false\n"
                "  runtime: true\n",
                "",
            ),
        )
        self.assert_code(
            "invalid_plugin_value",
            VALID_PAPER_V2.replace("  gateway: true", '  gateway: "true"'),
        )
        self.assert_code(
            "invalid_plugin_value",
            VALID_PAPER_V2.replace("  gateway: true", "  gateway: yes"),
        )

    def test_v2_dto_and_nested_values_validate_direct_construction(self):
        valid = parse_config(VALID_PAPER_V2)
        with self.assertRaises(RunConfigError):
            StrategyParameterList([1])
        with self.assertRaises(RunConfigError):
            StrategyParameterMap((("key", [1]),))
        with self.assertRaises(RunConfigError):
            ValidatedRunConfigV2(
                2,
                RunMode.PAPER,
                Environment.PAPER,
                valid.references,
                valid.plugins,
                (("window", [20]),),
                valid.requested_capabilities,
            )
        with self.assertRaises(RunConfigError):
            ValidatedRunConfigV2(
                2,
                RunMode.PAPER,
                Environment.PAPER,
                valid.references,
                valid.plugins,
                valid.strategy_parameters,
                ("orders.live.write",),
            )
        with self.assertRaises(TypeError):
            type("ForgedV2Config", (ValidatedRunConfigV2,), {})
        with self.assertRaises(TypeError):
            type("ForgedParameterList", (StrategyParameterList,), {})

    def test_v2_inherits_strict_yaml_duplicate_alias_and_tag_guards(self):
        duplicate = VALID_PAPER_V2.replace(
            "  product: product/binance_usdm\n",
            "  product: product/binance_usdm\n  product: product/other\n",
        )
        self.assert_code("duplicate_key", duplicate)
        self.assert_code("invalid_yaml", VALID_PAPER_V2 + "unused: &unused value\n")
        self.assert_code(
            "invalid_yaml",
            VALID_PAPER_V2.replace("window: 20", "window: !!python/object/apply:os.system ['whoami']"),
        )

    def test_v2_repr_redacts_references_and_strategy_parameter_values(self):
        marker = "credential/private_reference_marker"
        parameter_marker = "strategy-private-marker"
        source = VALID_PAPER_V2.replace("credential/paper_trading", marker).replace(
            "threshold: 0.5", 'threshold: "{}"'.format(parameter_marker)
        )
        value = parse_config(source)
        for rendered in (repr(value), repr(value.references)):
            self.assertNotIn(marker, rendered)
            self.assertNotIn(parameter_marker, rendered)

        invalid_reference = VALID_PAPER_V2.replace(
            "credential/paper_trading", "credential/private-reference-marker!"
        )
        with self.assertRaises(RunConfigError) as caught:
            parse_config(invalid_reference)
        for rendered in (str(caught.exception), repr(caught.exception), repr(caught.exception.args)):
            self.assertNotIn("private-reference-marker", rendered)

    def test_v2_parse_uses_only_the_supplied_data(self):
        originals = (
            builtins.__import__,
            builtins.open,
            os.getenv,
            socket.create_connection,
            urllib.request.urlopen,
            subprocess.Popen,
        )

        def reject(*args, **kwargs):
            raise AssertionError("schema parsing attempted an import or file access")

        try:
            builtins.__import__ = reject
            builtins.open = reject
            os.getenv = reject
            socket.create_connection = reject
            urllib.request.urlopen = reject
            subprocess.Popen = reject
            value = parse_config(VALID_PAPER_V2)
        finally:
            (
                builtins.__import__,
                builtins.open,
                os.getenv,
                socket.create_connection,
                urllib.request.urlopen,
                subprocess.Popen,
            ) = originals

        self.assertIs(type(value), ValidatedRunConfigV2)

    def test_mode_environment_mismatch_is_rejected(self):
        self.assert_code("environment_mismatch", _config("paper", "live"))
        self.assert_code("environment_mismatch", _config("live", "paper"))

    def test_deferred_modes_are_recognized_but_do_not_return_a_plan(self):
        for mode in ("replay", "shadow"):
            source = "schema_version: 1\nmode: {}\n".format(mode)
            self.assert_code("mode_deferred", source)

    def test_unknown_fields_and_missing_fields_are_rejected(self):
        self.assert_code("unknown_field", VALID_PAPER + "dry_run: true\n")
        self.assert_code("unknown_field", VALID_PAPER.replace("  provider:", "  import_path: x\n  provider:"))
        self.assert_code("missing_field", VALID_PAPER.replace("  scope: scope/usdm_paper_orders\n", ""))

    def test_schema_v4_is_rejected_without_legacy_paper_mapping(self):
        legacy = VALID_PAPER.replace("schema_version: 1", "schema_version: 4")
        self.assert_code("unsupported_schema_version", legacy)
        self.assert_code(
            "unknown_field",
            "schema_version: 1\nmode: paper\nenvironment: paper\n"
            "references: {}\nplugins: {}\npreset: paper\n",
        )

    def test_reference_fields_accept_only_typed_symbolic_ids(self):
        for unsafe in (
            "https://example.invalid/api",
            "module.Class",
            "credential/actual-secret-value!",
        ):
            source = VALID_PAPER.replace(
                "endpoint: endpoint/binance_paper", "endpoint: {}".format(unsafe)
            )
            self.assert_code("invalid_reference", source)

    def test_direct_construction_cannot_bypass_structure_or_deep_immutability(self):
        valid = parse_config(VALID_PAPER)
        with self.assertRaises(RunConfigError):
            ValidatedRunConfig(999, "live", Environment.PAPER, None, [("x", [])])
        with self.assertRaises(RunConfigError):
            ValidatedRunConfig(1, RunMode.LIVE, Environment.PAPER, valid.references, ())
        with self.assertRaises(RunConfigError):
            ValidatedRunConfig(1, RunMode.PAPER, Environment.PAPER, valid.references, (("x", []),))
        with self.assertRaises(RunConfigError):
            References("strategy/x", "provider/x", "endpoint/x", "account/x", "secret", "scope/x")

    def test_config_value_types_cannot_be_subclassed_to_bypass_validation(self):
        with self.assertRaises(TypeError):
            type("ForgedConfig", (ValidatedRunConfig,), {"__post_init__": lambda self: None})
        with self.assertRaises(TypeError):
            type("ForgedReferences", (References,), {"__post_init__": lambda self: None})

    def test_reference_values_are_not_displayed_by_object_repr(self):
        marker = "credential/reference_example_91ad"
        value = parse_config(VALID_PAPER.replace("credential/paper_trading", marker))
        self.assertEqual(value.references.credential, marker)
        self.assertNotIn(marker, repr(value))
        self.assertNotIn(marker, repr(value.references))

    def test_plugin_switches_require_explicit_yaml_booleans(self):
        self.assert_code("invalid_plugin_value", _config(plugins='  metrics: "true"\n'))
        self.assert_code("invalid_plugin_value", _config(plugins="  metrics: yes\n"))
        self.assert_code("invalid_plugin_value", _config(plugins="  metrics: on\n"))
        self.assert_code("invalid_plugin_name", _config(plugins="  BadName: true\n"))

    def test_duplicate_keys_are_rejected_at_every_mapping_level(self):
        duplicate_top = VALID_PAPER.replace("mode: paper\n", "mode: paper\nmode: live\n")
        duplicate_nested = VALID_PAPER.replace(
            "  provider: provider/binance_usdm\n",
            "  provider: provider/binance_usdm\n  provider: provider/other\n",
        )
        self.assert_code("duplicate_key", duplicate_top)
        self.assert_code("duplicate_key", duplicate_nested)

    def test_aliases_and_anchors_are_rejected_even_when_unused(self):
        self.assert_code("invalid_yaml", VALID_PAPER + "unused: &unused value\n")
        self.assert_code(
            "invalid_yaml",
            "schema_version: 1\nmode: paper\nenvironment: paper\n"
            "references: &refs {}\nplugins: {}\n",
        )
        self.assert_code(
            "invalid_yaml",
            "schema_version: 1\nmode: paper\nenvironment: paper\n"
            "references: &refs {}\nplugins: *refs\n",
        )

    def test_multiple_documents_custom_tags_and_non_mapping_roots_fail_closed(self):
        self.assert_code("multiple_documents", VALID_PAPER + "---\n{}\n")
        self.assert_code("invalid_yaml", "schema_version: !!python/object/apply:os.system ['whoami']\n")
        self.assert_code("invalid_yaml", _config(plugins="  metrics: !!bool yes\n"))
        self.assert_code("invalid_schema", "[schema_version, 1]\n")
        self.assert_code("invalid_yaml", "")

    def test_yaml_nesting_and_event_count_are_bounded(self):
        nested = "[" * 34 + "0" + "]" * 34
        self.assert_code("document_too_complex", nested)

        many_scalars = "items: [" + ",".join("x" for _ in range(8300)) + "]\n"
        self.assert_code("document_too_complex", many_scalars)

    def test_invalid_utf8_input_types_and_oversized_inputs_are_rejected(self):
        self.assert_code("invalid_utf8", b"schema_version: \xff")
        self.assert_code("invalid_utf8", "\ud800")
        self.assert_code("invalid_input_type", bytearray(b"schema_version: 1"))
        self.assert_code("input_too_large", b" " * (MAX_CONFIG_BYTES + 1))
        self.assert_code("input_too_large", " " * (MAX_CONFIG_BYTES + 1))

    def test_parser_errors_do_not_reflect_yaml_or_secret_values(self):
        secret_marker = "private-value-not-for-diagnostics-91ad"
        malformed = "schema_version: 1\ninvalid: [\n" + secret_marker
        with self.assertRaises(RunConfigError) as caught:
            parse_config(malformed)
        self.assertEqual(caught.exception.code, "invalid_yaml")
        self.assertNotIn(secret_marker, str(caught.exception))
        self.assertNotIn(secret_marker, repr(caught.exception))
        self.assertNotIn(secret_marker, repr(caught.exception.args))
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
