# bt-api-run-contract

This directory is an independent build root for the `bt_api_run_contract`
package. It has one versioned YAML schema and depends at runtime only on
PyYAML and the Python standard library. Package metadata is version `0.1.1` for
this source candidate; this change is not a release.

Schema version 1 accepts `paper` and `live` configuration documents. The
`mode` and `environment` must match. The names `replay` and `shadow` are
recognized as deferred modes; parsing either raises `RunConfigError` with code
`mode_deferred` and never returns a run configuration. This package does not
run a strategy or implement any mode.

Example:

```yaml
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
```

Reference values are symbolic identifiers, not Python import paths, endpoint
URLs, account data, or credentials. Parsing does not establish that a
reference exists, is trustworthy, matches the selected provider, or grants any
permission. A later trusted registry/authority must validate every reference
and the requested scope. Plugin identifiers are also symbolic registry keys;
this parser checks their shape and strict YAML boolean values but does not
resolve or import plugins.

Reference spelling alone cannot distinguish an identifier from secret material.
Deployments must use opaque identifiers, prohibit secret values in config, and
validate references with a trusted registry; object representations omit them.
Validated data is not a provenance seal or authorization, even if constructed
directly. An undeclared plugin lookup raises instead of silently returning off.

The contracts package also provides a minimal in-memory route-binding
candidate. Deployment code may explicitly construct `TrustedRoute` values from
an `Environment` and a complete `References` bundle, place a non-empty tuple in
`TrustedRegistry`, then call `resolve_plan(config, registry)`. Multiple accounts
and scopes may share a provider/environment. The complete six-reference bundle
must be unique. Resolution checks the explicit environment and all six
references as one binding; it does not infer an environment from identifier
spelling or fall back to another route. A registry rejects reuse of endpoint,
account, credential, or scope reference identifiers across paper and live;
provider and strategy identifiers may be shared. Missing, cross-environment,
duplicate, and mismatched bindings fail with fixed, value-free errors. The
returned tuple-backed `ResolvedPlan` contains only mode, environment, and
symbolic references; credential references remain identifiers, not credential
values. Ordinary direct construction is disabled. Call
`validate_plan(plan, config, registry)` immediately before consuming the static
result. The guard runs the complete resolver again, including the enabled
plugin rejection, then compares mode, environment, and all six references.

`TrustedRoute` and `TrustedRegistry` are tuple-backed values and record the
deployment caller's trust assertion; their types do not establish provenance or
authority. Their `References` views are fresh immutable copies. `resolve_plan`
and `validate_plan(plan, config, registry)` perform static reference matching
only. They grant no order permission and do not import or call strategies,
providers, credential sources, endpoints, factories, or plugins. Any enabled
plugin currently fails with `plugin_resolution_deferred`; plugin registration,
availability, and dependency checks remain a separate fail-closed design.
Disabled plugin declarations are not copied into `ResolvedPlan`.

The current registry contains symbolic strings only; it has no authoritative
account, endpoint, credential, or strategy objects whose identities it could
verify. Cross-environment reference separation checks identifier reuse, not
the real-world meaning or safety of those identifiers. Environment identity,
credential custody, provider compatibility, and order permission remain
unverified and require a separate authoritative deployment contract.

Python object mechanics are not a security boundary. `ResolvedPlan` is a
non-authoritative data result; its ordinary constructor is disabled and callers
must recheck it with `validate_plan(plan, config, registry)` against the current
injected registry before consuming it. That check reuses the config's plugin
gate and proves only a static symbolic match.

`parse_config` only accepts caller-provided `str` or UTF-8 `bytes`. It performs
no file access, network access, provider import, strategy import, or execution.
It rejects unknown fields, duplicate mapping keys, aliases/anchors, multiple
documents, custom YAML tags, invalid UTF-8, and inputs beyond fixed size and
complexity limits. Diagnostics contain fixed safe messages and never include
the source text or rejected values. Schema-v4 documents are rejected; there is
no automatic legacy `paper` mapping.

The returned `ValidatedRunConfig` and all nested values are immutable. It is a
pure-data candidate, not an authorized or resolved execution plan. This source
package and its fake/boundary tests do not establish an installed consumer,
provider integration, external paper-account acceptance, or live-write
authorization.

Run the focused tests from this directory with:

```powershell
python -m pytest -q
```

The public import name is `bt_api_run_contract` from both the source tree and
an installed wheel:

```python
from bt_api_run_contract import parse_config

config = parse_config(yaml_bytes_or_text)
```
