import builtins
import copy
import json
import pickle
import unittest
from dataclasses import FrozenInstanceError

from bt_api_run_contract import (
    Environment,
    References,
    ResolvedPlan,
    RunConfigError,
    RunMode,
    TrustedRegistry,
    TrustedRoute,
    ValidatedRunConfig,
    ValidatedRunConfigV2,
    parse_config,
    resolve_plan,
    validate_plan,
)


_BASE = {
    "strategy": "strategy/shared_alpha",
    "provider": "provider/example",
    "endpoint": "endpoint/paper_primary",
    "account": "account/paper_primary",
    "credential": "credential/paper_primary",
    "scope": "scope/paper_orders",
}


def _references(**changes):
    values = dict(_BASE)
    values.update(changes)
    return References(**values)


def _config(references, environment=Environment.PAPER, plugins=()):
    return ValidatedRunConfig(
        schema_version=1,
        mode=RunMode(environment.value),
        environment=environment,
        references=references,
        plugins=tuple(sorted(plugins)),
    )


def _paper_live_registry():
    paper = TrustedRoute(Environment.PAPER, _references())
    live = TrustedRoute(
        Environment.LIVE,
        _references(
            endpoint="endpoint/live_primary",
            account="account/live_primary",
            credential="credential/live_primary",
            scope="scope/live_orders",
        ),
    )
    return paper, live, TrustedRegistry((paper, live))


class TrustedRegistryTests(unittest.TestCase):
    def assert_code(self, code, callback, *args):
        with self.assertRaises(RunConfigError) as caught:
            callback(*args)
        self.assertEqual(caught.exception.code, code)
        self.assertIsNone(caught.exception.__cause__)
        return caught.exception

    def test_same_strategy_reference_resolves_distinct_paper_and_live_bindings(self):
        paper, live, registry = _paper_live_registry()
        paper_plan = resolve_plan(_config(paper.references), registry)
        live_plan = resolve_plan(
            _config(live.references, environment=Environment.LIVE), registry
        )

        self.assertEqual(paper_plan.mode, RunMode.PAPER)
        self.assertEqual(paper_plan.environment, Environment.PAPER)
        self.assertEqual(paper_plan.references, paper.references)
        self.assertEqual(live_plan.mode, RunMode.LIVE)
        self.assertEqual(live_plan.environment, Environment.LIVE)
        self.assertEqual(live_plan.references, live.references)
        self.assertEqual(paper_plan.references.strategy, live_plan.references.strategy)
        self.assertEqual(paper_plan.references.provider, live_plan.references.provider)
        self.assertNotEqual(paper_plan.references.account, live_plan.references.account)
        self.assertIs(
            validate_plan(paper_plan, _config(paper.references), registry),
            paper_plan,
        )

    def test_multiple_accounts_and_scopes_can_share_a_provider_environment(self):
        first = TrustedRoute(Environment.PAPER, _references())
        second = TrustedRoute(
            Environment.PAPER,
            _references(
                account="account/paper_secondary",
                credential="credential/paper_secondary",
                scope="scope/paper_secondary_orders",
            ),
        )
        registry = TrustedRegistry((first, second))

        first_plan = resolve_plan(_config(first.references), registry)
        second_plan = resolve_plan(_config(second.references), registry)

        self.assertEqual(first_plan.references.account, "account/paper_primary")
        self.assertEqual(second_plan.references.account, "account/paper_secondary")
        self.assertEqual(first_plan.references.scope, "scope/paper_orders")
        self.assertEqual(second_plan.references.scope, "scope/paper_secondary_orders")

    def test_each_reference_must_match_the_complete_registered_binding(self):
        base = _references()
        alternate = _references(
            strategy="strategy/alternate",
            provider="provider/alternate",
            endpoint="endpoint/alternate",
            account="account/alternate",
            credential="credential/alternate",
            scope="scope/alternate",
        )
        registry = TrustedRegistry(
            (
                TrustedRoute(Environment.PAPER, base),
                TrustedRoute(Environment.PAPER, alternate),
            )
        )

        fields = ("strategy", "provider", "endpoint", "account", "credential", "scope")
        for field in fields:
            with self.subTest(field=field):
                mismatch = _references(**{field: getattr(alternate, field)})
                self.assert_code("route_mismatch", resolve_plan, _config(mismatch), registry)

    def test_environment_is_explicit_and_cross_environment_routes_do_not_fallback(self):
        paper, live, both_envs = _paper_live_registry()

        # The config is internally consistent (live/live), but the only route
        # supplied by deployment code is the paper route for this provider.
        paper_only = TrustedRegistry((paper,))
        self.assert_code(
            "route_environment_mismatch",
            resolve_plan,
            _config(paper.references, environment=Environment.LIVE),
            paper_only,
        )

        # With both environments registered, paper references still cannot
        # select the live binding by provider name alone.
        self.assert_code(
            "route_mismatch",
            resolve_plan,
            _config(paper.references, environment=Environment.LIVE),
            both_envs,
        )
        self.assertEqual(live.environment, Environment.LIVE)

    def test_unregistered_provider_and_empty_registry_fail_closed(self):
        route = TrustedRoute(Environment.PAPER, _references())
        registry = TrustedRegistry((route,))
        unregistered = _references(provider="provider/unregistered")
        self.assert_code("unregistered_route", resolve_plan, _config(unregistered), registry)
        self.assert_code("empty_registry", TrustedRegistry, ())
        self.assert_code("invalid_registry", TrustedRegistry, [])

    def test_same_provider_environment_requires_a_unique_complete_binding(self):
        first = TrustedRoute(Environment.PAPER, _references())
        second = TrustedRoute(
            Environment.PAPER,
            _references(
                endpoint="endpoint/other",
                account="account/other",
                credential="credential/other",
                scope="scope/other",
            ),
        )
        registry = TrustedRegistry((first, second))
        self.assertEqual(
            resolve_plan(_config(first.references), registry).references,
            first.references,
        )
        self.assertEqual(
            resolve_plan(_config(second.references), registry).references,
            second.references,
        )
        self.assert_code("duplicate_route", TrustedRegistry, (first, first))

    def test_sensitive_reference_ids_cannot_be_reused_across_environments(self):
        paper = _references()
        for field in ("endpoint", "account", "credential", "scope"):
            with self.subTest(field=field):
                live_values = {
                    "endpoint": "endpoint/live_primary",
                    "account": "account/live_primary",
                    "credential": "credential/live_primary",
                    "scope": "scope/live_orders",
                }
                live_values[field] = getattr(paper, field)
                live = _references(**live_values)
                self.assert_code(
                    "cross_environment_reference",
                    TrustedRegistry,
                    (
                        TrustedRoute(Environment.PAPER, paper),
                        TrustedRoute(Environment.LIVE, live),
                    ),
                )

    def test_invalid_resolution_input_and_registry_entries_are_rejected(self):
        route = TrustedRoute(Environment.PAPER, _references())
        registry = TrustedRegistry((route,))
        self.assert_code("invalid_resolution_input", resolve_plan, {}, registry)
        self.assert_code(
            "invalid_registry",
            resolve_plan,
            _config(route.references),
            object(),
        )
        self.assert_code("invalid_route", TrustedRegistry, (object(),))
        self.assert_code("invalid_route", TrustedRoute, "paper", route.references)
        self.assert_code("invalid_route", TrustedRoute)
        self.assert_code("invalid_registry", TrustedRegistry)

    def test_v2_resolution_is_explicitly_deferred_without_changing_v1_plan_tuple(self):
        config_v2 = parse_config(
            "schema_version: 2\n"
            "mode: paper\n"
            "environment: paper\n"
            "references:\n"
            "  strategy: strategy/example\n"
            "  provider: provider/example\n"
            "  endpoint: endpoint/example\n"
            "  account: account/example\n"
            "  credential: credential/example\n"
            "  scope: scope/example\n"
            "  product: product/example\n"
            "  data_source: data_source/example\n"
            "  clock: clock/example\n"
            "plugins: {}\n"
            "strategy_parameters: {}\n"
            "requested_capabilities: []\n"
        )
        self.assertIs(type(config_v2), ValidatedRunConfigV2)
        self.assert_code("schema_v2_resolution_deferred", resolve_plan, config_v2, object())

        route = TrustedRoute(Environment.PAPER, _references())
        registry = TrustedRegistry((route,))
        config_v1 = _config(route.references)
        plan = resolve_plan(config_v1, registry)
        self.assertEqual(tuple.__len__(plan), 8)
        self.assertEqual(
            tuple.__getitem__(plan, slice(0, 2)),
            (RunMode.PAPER, Environment.PAPER),
        )
        self.assert_code("schema_v2_resolution_deferred", validate_plan, plan, config_v2, registry)

    def test_enabled_plugins_are_explicitly_deferred(self):
        route = TrustedRoute(Environment.PAPER, _references())
        registry = TrustedRegistry((route,))
        config = _config(route.references, plugins=(("trade_logger", True),))
        self.assert_code("plugin_resolution_deferred", resolve_plan, config, registry)

        disabled = _config(route.references, plugins=(("trade_logger", False),))
        plan = resolve_plan(disabled, registry)
        self.assertFalse(hasattr(plan, "plugins"))

        forged = tuple.__new__(
            ResolvedPlan,
            (RunMode.PAPER, Environment.PAPER)
            + tuple(
                getattr(route.references, field)
                for field in ("strategy", "provider", "endpoint", "account", "credential", "scope")
            ),
        )
        self.assert_code(
            "plugin_resolution_deferred",
            validate_plan,
            forged,
            config,
            registry,
        )

    def test_registry_route_and_resolved_plan_are_immutable(self):
        route = TrustedRoute(Environment.PAPER, _references())
        registry = TrustedRegistry((route,))
        plan = resolve_plan(_config(route.references), registry)

        with self.assertRaises(AttributeError):
            route.environment = Environment.LIVE
        with self.assertRaises(AttributeError):
            registry.routes = ()
        with self.assertRaises(TypeError):
            registry.routes[0] = route
        with self.assertRaises(AttributeError):
            plan.mode = RunMode.LIVE
        with self.assertRaises(FrozenInstanceError):
            plan.references.credential = "credential/other"

        for obj, name, value in (
            (route, "environment", Environment.LIVE),
            (registry, "routes", ()),
            (plan, "mode", RunMode.LIVE),
        ):
            with self.subTest(name=name):
                with self.assertRaises(AttributeError):
                    object.__setattr__(obj, name, value)

    def test_plan_copies_references_and_only_resolver_constructs_it_normally(self):
        references = _references()
        route = TrustedRoute(Environment.PAPER, references)
        registry = TrustedRegistry((route,))
        config = _config(references)
        plan = resolve_plan(config, registry)
        original_credential = "credential/paper_primary"

        self.assertIsNot(plan.references, references)
        self.assertIsNot(plan.references, route.references)
        view = plan.references
        object.__setattr__(view, "credential", "credential/tampered_view")
        object.__setattr__(references, "credential", "credential/tampered_config")
        self.assertEqual(plan.references.credential, original_credential)
        self.assertEqual(route.references.credential, original_credential)
        self.assertIs(validate_plan(plan, _config(route.references), registry), plan)

        self.assert_code(
            "direct_plan_construction",
            ResolvedPlan,
            RunMode.PAPER,
            Environment.PAPER,
            references,
        )
        other = TrustedRegistry(
            (TrustedRoute(Environment.PAPER, _references(provider="provider/other")),)
        )
        self.assert_code(
            "unregistered_route", validate_plan, plan, _config(route.references), other
        )

    def test_public_guard_sanitizes_malformed_uninitialized_objects(self):
        route = TrustedRoute(Environment.PAPER, _references())
        registry = TrustedRegistry((route,))
        plan = resolve_plan(_config(route.references), registry)

        empty_config = object.__new__(ValidatedRunConfig)
        empty_route = tuple.__new__(TrustedRoute, ())
        empty_registry = tuple.__new__(TrustedRegistry, ())
        empty_plan = tuple.__new__(ResolvedPlan, ())

        self.assert_code("invalid_resolution_input", resolve_plan, empty_config, registry)
        self.assert_code("invalid_route", TrustedRegistry, (empty_route,))
        config = _config(route.references)
        self.assert_code("empty_registry", validate_plan, plan, config, empty_registry)
        self.assert_code(
            "invalid_resolution_input", validate_plan, empty_plan, config, registry
        )

    def test_public_guard_rejects_low_level_plan_with_mismatched_references(self):
        route = TrustedRoute(Environment.PAPER, _references())
        registry = TrustedRegistry((route,))
        forged_references = _references(account="account/forged")
        forged_values = (RunMode.PAPER, Environment.PAPER) + tuple(
            getattr(forged_references, field)
            for field in ("strategy", "provider", "endpoint", "account", "credential", "scope")
        )
        forged = tuple.__new__(ResolvedPlan, forged_values)

        self.assert_code(
            "plan_mismatch", validate_plan, forged, _config(route.references), registry
        )

    def test_public_guard_rejects_a_different_config_binding(self):
        first = TrustedRoute(Environment.PAPER, _references())
        second = TrustedRoute(
            Environment.PAPER,
            _references(
                account="account/alternate",
                credential="credential/alternate",
                scope="scope/alternate",
            ),
        )
        registry = TrustedRegistry((first, second))
        plan = resolve_plan(_config(first.references), registry)

        self.assert_code(
            "plan_mismatch",
            validate_plan,
            plan,
            _config(second.references),
            registry,
        )

    def test_values_reject_implicit_json_pickle_and_copy_serialization(self):
        route = TrustedRoute(Environment.PAPER, _references())
        registry = TrustedRegistry((route,))
        plan = resolve_plan(_config(route.references), registry)
        nested = {"route": [route], "registry": {"value": registry}, "plan": plan}

        for value in (route, registry, plan, nested):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(TypeError) as caught:
                    json.dumps(value)
                self.assertNotIn("credential/paper_primary", str(caught.exception))

        for value in (route, registry, plan):
            with self.subTest(copy_type=type(value).__name__):
                with self.assertRaises(TypeError):
                    pickle.dumps(value)
                with self.assertRaises(TypeError):
                    copy.copy(value)
                with self.assertRaises(TypeError):
                    copy.deepcopy(value)

        self.assertEqual(registry.routes, (route,))
        self.assertEqual(plan.references, route.references)
        self.assertIs(validate_plan(plan, _config(route.references), registry), plan)

    def test_representations_and_errors_do_not_disclose_references(self):
        marker = "credential/private_reference_marker"
        references = _references(credential=marker)
        route = TrustedRoute(Environment.PAPER, references)
        registry = TrustedRegistry((route,))
        plan = resolve_plan(_config(references), registry)

        for value in (route, registry, plan):
            self.assertNotIn(marker, repr(value))
        mismatch = _references(credential="credential/different")
        error = self.assert_code("route_mismatch", resolve_plan, _config(mismatch), registry)
        for rendered in (str(error), repr(error), repr(error.args)):
            self.assertNotIn(marker, rendered)

    def test_resolution_does_not_import_or_open_files(self):
        route = TrustedRoute(Environment.PAPER, _references())
        registry = TrustedRegistry((route,))
        config = _config(route.references)
        original_import = builtins.__import__
        original_open = builtins.open

        def reject(*args, **kwargs):
            raise AssertionError("resolution attempted an import or file access")

        try:
            builtins.__import__ = reject
            builtins.open = reject
            plan = resolve_plan(config, registry)
        finally:
            builtins.__import__ = original_import
            builtins.open = original_open

        self.assertEqual(plan.references, route.references)


if __name__ == "__main__":
    unittest.main()
