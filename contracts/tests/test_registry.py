import builtins
import copy
import io
import json
import pickle
import unittest
from contextlib import redirect_stdout
from dataclasses import FrozenInstanceError

from bt_api_run_contract import (
    Environment,
    References,
    ReferencesV2,
    ResolvedPlan,
    ResolvedPlanV2,
    RunConfigError,
    RunMode,
    StrategyParameterList,
    StrategyParameterMap,
    TrustedRegistry,
    TrustedRegistryV2,
    TrustedRoute,
    TrustedRouteV2,
    ValidatedRunConfig,
    ValidatedRunConfigV2,
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
_BASE_V2 = {
    "strategy": "strategy/shared_alpha",
    "provider": "provider/example",
    "endpoint": "endpoint/paper_primary",
    "account": "account/paper_primary",
    "credential": "credential/paper_primary",
    "scope": "scope/paper_orders",
    "product": "product/example",
    "data_source": "data_source/market_feed",
    "clock": "clock/exchange_utc",
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


def _references_v2(**changes):
    values = dict(_BASE_V2)
    values.update(changes)
    return ReferencesV2(**values)


def _config_v2(
    references=None,
    environment=Environment.PAPER,
    parameters=(("window", 20),),
    capabilities=("market.read",),
    plugins=(),
):
    if references is None:
        references = _references_v2()
    return ValidatedRunConfigV2(
        schema_version=2,
        mode=RunMode(environment.value),
        environment=environment,
        references=references,
        plugins=tuple(sorted(plugins)),
        strategy_parameters=tuple(sorted(parameters)),
        requested_capabilities=tuple(sorted(capabilities)),
    )


def _route_v2(
    references=None,
    environment=Environment.PAPER,
    capabilities=("account.read", "market.read"),
    parameter_contract=(("window", "int", True),),
):
    if references is None:
        references = _references_v2()
    return TrustedRouteV2(
        environment,
        references,
        tuple(sorted(capabilities)),
        tuple(sorted(parameter_contract)),
    )


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

    def test_v1_and_v2_dispatch_to_separate_plan_types_and_keep_v1_tuple_shape(self):
        route = TrustedRoute(Environment.PAPER, _references())
        registry = TrustedRegistry((route,))
        config_v1 = _config(route.references)
        plan_v1 = resolve_plan(config_v1, registry)
        self.assertIs(type(plan_v1), ResolvedPlan)
        self.assertEqual(tuple.__len__(plan_v1), 8)
        self.assertEqual(
            tuple.__getitem__(plan_v1, slice(0, 2)),
            (RunMode.PAPER, Environment.PAPER),
        )
        self.assertIs(validate_plan(plan_v1, config_v1, registry), plan_v1)

        route_v2 = _route_v2()
        registry_v2 = TrustedRegistryV2((route_v2,))
        config_v2 = _config_v2()
        plan_v2 = resolve_plan(config_v2, registry_v2)
        self.assertIs(type(config_v2), ValidatedRunConfigV2)
        self.assertIs(type(plan_v2), ResolvedPlanV2)
        self.assertEqual(tuple.__len__(plan_v2), 13)
        self.assertIs(validate_plan(plan_v2, config_v2, registry_v2), plan_v2)

        self.assert_code("invalid_registry", resolve_plan, config_v1, registry_v2)
        self.assert_code("invalid_registry", resolve_plan, config_v2, registry)
        self.assert_code("plan_version_mismatch", validate_plan, plan_v1, config_v2, registry_v2)
        self.assert_code("plan_version_mismatch", validate_plan, plan_v2, config_v1, registry)

    def test_v2_route_binding_checks_all_nine_references_as_one_bundle(self):
        base = _references_v2()
        alternate_values = {
            field: "{}/alternate".format(field)
            for field in (
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
        }
        alternate = _references_v2(**alternate_values)
        registry = TrustedRegistryV2((_route_v2(base), _route_v2(alternate)))

        for field in _BASE_V2:
            with self.subTest(field=field):
                mismatch = _references_v2(**{field: getattr(alternate, field)})
                self.assert_code(
                    "route_mismatch",
                    resolve_plan,
                    _config_v2(mismatch),
                    registry,
                )

        # Combining selected fields from two routes cannot synthesize a third.
        mixed = _references_v2(
            strategy=base.strategy,
            provider=base.provider,
            endpoint=base.endpoint,
            account=alternate.account,
            credential=base.credential,
            scope=base.scope,
            product=alternate.product,
            data_source=base.data_source,
            clock=alternate.clock,
        )
        self.assert_code("route_mismatch", resolve_plan, _config_v2(mixed), registry)

    def test_v2_environment_is_explicit_and_never_falls_back(self):
        route = _route_v2()
        registry = TrustedRegistryV2((route,))
        self.assert_code(
            "route_environment_mismatch",
            resolve_plan,
            _config_v2(environment=Environment.LIVE, references=route.references),
            registry,
        )

    def test_v2_registry_rejects_cross_environment_sensitive_references(self):
        paper = _references_v2()
        live = _references_v2(
            endpoint="endpoint/live",
            account="account/live",
            credential=paper.credential,
            scope="scope/live",
        )
        self.assert_code(
            "cross_environment_reference",
            TrustedRegistryV2,
            (_route_v2(paper), _route_v2(live, environment=Environment.LIVE)),
        )

    def test_v2_capability_declarations_are_subset_checks_and_writes_stay_deferred(self):
        route = _route_v2(capabilities=("account.read", "market.read"))
        registry = TrustedRegistryV2((route,))
        plan = resolve_plan(
            _config_v2(capabilities=("market.read", "account.read")), registry
        )
        self.assertEqual(plan.requested_capabilities, ("account.read", "market.read"))

        self.assert_code(
            "capability_not_declared",
            resolve_plan,
            _config_v2(capabilities=("account.read",)),
            TrustedRegistryV2(
                (_route_v2(capabilities=("market.read",)),)
            ),
        )

        write_route = _route_v2(capabilities=("market.read", "orders.paper.write"))
        write_registry = TrustedRegistryV2((write_route,))
        write_config = _config_v2(capabilities=("orders.paper.write",))
        self.assert_code(
            "capability_resolution_deferred", resolve_plan, write_config, write_registry
        )
        self.assert_code(
            "capability_resolution_deferred",
            validate_plan,
            plan,
            write_config,
            write_registry,
        )

    def test_v2_enabled_plugins_remain_deferred(self):
        registry = TrustedRegistryV2((_route_v2(),))
        config = _config_v2(plugins=(("gateway", True),))
        self.assert_code("plugin_resolution_deferred", resolve_plan, config, registry)
        disabled = resolve_plan(_config_v2(plugins=(("gateway", False),)), registry)
        self.assertEqual(disabled.references, _references_v2())

    def test_v2_parameter_contract_checks_required_unknown_exact_types_and_nested_values(self):
        contract = (
            ("enabled", "bool", True),
            ("window", "int", True),
        )
        route = _route_v2(parameter_contract=contract)
        registry = TrustedRegistryV2((route,))

        self.assert_code(
            "missing_strategy_parameter",
            resolve_plan,
            _config_v2(parameters=(("window", 20),)),
            registry,
        )
        self.assert_code(
            "unknown_strategy_parameter",
            resolve_plan,
            _config_v2(parameters=(("enabled", True), ("mystery", 1), ("window", 20))),
            registry,
        )
        self.assert_code(
            "strategy_parameter_type_mismatch",
            resolve_plan,
            _config_v2(parameters=(("enabled", True), ("window", True))),
            registry,
        )
        self.assert_code(
            "strategy_parameter_type_mismatch",
            resolve_plan,
            _config_v2(parameters=(("enabled", 1), ("window", 20))),
            registry,
        )

        values = (
            ("enabled", True),
            ("window", 20),
            ("watchlist", StrategyParameterList(("BTCUSDT", "ETHUSDT"))),
            ("sizing", StrategyParameterMap((("max_positions", 4),))),
            ("optional", None),
        )
        full_contract = (
            ("enabled", "bool", True),
            ("optional", "null", False),
            ("sizing", "map", True),
            ("watchlist", "list", True),
            ("window", "int", True),
        )
        full_registry = TrustedRegistryV2((_route_v2(parameter_contract=full_contract),))
        config = _config_v2(parameters=values)
        plan = resolve_plan(config, full_registry)
        plan_parameters = tuple.__getitem__(plan, 12)
        self.assertEqual(plan_parameters, config.strategy_parameters)
        self.assertIsNot(plan_parameters[2][1], config.strategy_parameters[2][1])

        for invalid_kind in ("callable", lambda value: True):
            with self.subTest(kind=type(invalid_kind).__name__):
                self.assert_code(
                    "invalid_route",
                    lambda: _route_v2(
                        parameter_contract=(("window", invalid_kind, True),)
                    ),
                )

        calls = []

        def forbidden_parameter_callback(value):
            calls.append(value)

        self.assert_code(
            "invalid_route",
            lambda: _route_v2(
                parameter_contract=(("window", forbidden_parameter_callback, True),)
            ),
        )
        self.assertEqual(calls, [])

    def test_v2_revalidation_rechecks_plan_contents_registry_and_parameter_contract(self):
        route = _route_v2()
        registry = TrustedRegistryV2((route,))
        config = _config_v2(parameters=(("window", 1),))
        plan = resolve_plan(config, registry)
        self.assertIs(validate_plan(plan, config, registry), plan)

        forged_values = tuple.__getitem__(plan, slice(None))
        forged = tuple.__new__(ResolvedPlanV2, forged_values[:-1] + ((('window', 21),),))
        self.assert_code("plan_mismatch", validate_plan, forged, config, registry)
        for value in (True, 1.0):
            typed_forgery = tuple.__new__(
                ResolvedPlanV2,
                forged_values[:-1] + ((('window', value),),),
            )
            with self.subTest(forged_type=type(value).__name__):
                self.assert_code(
                    "plan_mismatch", validate_plan, typed_forgery, config, registry
                )
        changed_config = _config_v2(parameters=(("window", 2),))
        self.assert_code("plan_mismatch", validate_plan, plan, changed_config, registry)

        narrowed = TrustedRegistryV2((_route_v2(capabilities=("account.read",)),))
        self.assert_code("capability_not_declared", validate_plan, plan, config, narrowed)

        nested_references = _references_v2()
        nested_contract = (("settings", "map", True),)
        nested_registry = TrustedRegistryV2(
            (_route_v2(nested_references, parameter_contract=nested_contract),)
        )
        nested_config = _config_v2(
            references=nested_references,
            parameters=(("settings", StrategyParameterMap((("quantity", 1),))),),
        )
        nested_plan = resolve_plan(nested_config, nested_registry)
        nested_parts = tuple.__getitem__(nested_plan, slice(None))
        nested_forgery = tuple.__new__(
            ResolvedPlanV2,
            nested_parts[:-1]
            + ((('settings', StrategyParameterMap((("quantity", True),))),),),
        )
        self.assert_code(
            "plan_mismatch",
            validate_plan,
            nested_forgery,
            nested_config,
            nested_registry,
        )

        float_references = _references_v2()
        float_registry = TrustedRegistryV2(
            (
                _route_v2(
                    float_references,
                    parameter_contract=(("threshold", "float", True),),
                ),
            )
        )
        negative_zero_config = _config_v2(
            references=float_references,
            parameters=(("threshold", -0.0),),
        )
        negative_zero_plan = resolve_plan(negative_zero_config, float_registry)
        zero_parts = tuple.__getitem__(negative_zero_plan, slice(None))
        positive_zero_forgery = tuple.__new__(
            ResolvedPlanV2,
            zero_parts[:-1] + ((('threshold', 0.0),),),
        )
        self.assert_code(
            "plan_mismatch",
            validate_plan,
            positive_zero_forgery,
            negative_zero_config,
            float_registry,
        )

    def test_v2_bindings_and_plans_are_immutable_and_redact_sensitive_values(self):
        marker = "private-marker-91ad"
        references = _references_v2(
            **{field: "{}/{}".format(field, marker) for field in _BASE_V2}
        )
        route = _route_v2(
            references=references,
            capabilities=("market.read",),
            parameter_contract=(("label", "str", True),),
        )
        registry = TrustedRegistryV2((route,))
        config = _config_v2(
            references=references,
            parameters=(("label", marker),),
        )
        plan = resolve_plan(config, registry)

        for value in (references, route, registry, config, plan, plan.references):
            self.assertNotIn(marker, repr(value))
            self.assertNotIn(marker, str(value))
        mismatch_values = {
            field: getattr(references, field) for field in _BASE_V2
        }
        mismatch_values["credential"] = "credential/other"
        error = self.assert_code(
            "route_mismatch",
            resolve_plan,
            _config_v2(
                references=ReferencesV2(**mismatch_values),
                parameters=(("label", marker),),
            ),
            registry,
        )
        for rendered in (str(error), repr(error), repr(error.args)):
            self.assertNotIn(marker, rendered)

        self.assert_code(
            "direct_plan_construction",
            ResolvedPlanV2,
            RunMode.PAPER,
            Environment.PAPER,
        )
        with self.assertRaises(AttributeError):
            route.environment = Environment.LIVE
        with self.assertRaises(TypeError):
            registry.routes[0] = route
        with self.assertRaises(AttributeError):
            plan.mode = RunMode.LIVE
        with self.assertRaises(FrozenInstanceError):
            plan.references.credential = "credential/other"

        for value in (route, registry, plan):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(TypeError):
                    json.dumps(value)
                with self.assertRaises(TypeError):
                    pickle.dumps(value)
                with self.assertRaises(TypeError):
                    copy.copy(value)
                with self.assertRaises(TypeError):
                    copy.deepcopy(value)

    def test_v2_rejects_deep_malformed_parameter_shells_and_prints_nothing(self):
        nested = StrategyParameterMap(
            (("targets", StrategyParameterList(("BTCUSDT",))),)
        )
        contract = (("settings", "map", True),)
        route = _route_v2(parameter_contract=contract)
        registry = TrustedRegistryV2((route,))
        config = _config_v2(parameters=(("settings", nested),))
        plan = resolve_plan(config, registry)
        self.assertEqual(tuple.__getitem__(plan, 12)[0][0], "settings")

        empty_list = object.__new__(StrategyParameterList)
        object.__setattr__(nested, "entries", (("targets", empty_list),))
        object.__setattr__(config, "strategy_parameters", (("settings", nested),))
        self.assert_code("invalid_resolution_input", resolve_plan, config, registry)

        valid_settings = StrategyParameterMap(
            (("targets", StrategyParameterList(("BTCUSDT",))),)
        )
        valid_config = _config_v2(parameters=(("settings", valid_settings),))
        valid_plan = resolve_plan(valid_config, registry)
        self.assertNotIn("BTCUSDT", repr(valid_config))
        self.assertNotIn("BTCUSDT", repr(valid_plan))
        malformed_parts = tuple.__getitem__(valid_plan, slice(None))
        malformed_plan = tuple.__new__(
            ResolvedPlanV2,
            malformed_parts[:-1] + ((("settings", nested),),),
        )
        self.assert_code(
            "invalid_resolution_input", validate_plan, malformed_plan, valid_config, registry
        )

        output = io.StringIO()
        with redirect_stdout(output):
            resolved = resolve_plan(valid_config, registry)
            checked = validate_plan(valid_plan, valid_config, registry)
            repr(checked)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("BTCUSDT", repr(resolved))
        self.assertIs(checked, valid_plan)

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
        route_v2 = _route_v2()
        registry_v2 = TrustedRegistryV2((route_v2,))
        config_v2 = _config_v2()
        original_import = builtins.__import__
        original_open = builtins.open

        def reject(*args, **kwargs):
            raise AssertionError("resolution attempted an import or file access")

        try:
            builtins.__import__ = reject
            builtins.open = reject
            plan = resolve_plan(config, registry)
            plan_v2 = resolve_plan(config_v2, registry_v2)
        finally:
            builtins.__import__ = original_import
            builtins.open = original_open

        self.assertEqual(plan.references, route.references)
        self.assertEqual(plan_v2.references, route_v2.references)


if __name__ == "__main__":
    unittest.main()
