import unittest
from dataclasses import FrozenInstanceError

from bt_api_run_contract import (
    Environment,
    MAX_CONFIG_BYTES,
    References,
    RunConfigError,
    RunMode,
    ValidatedRunConfig,
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
