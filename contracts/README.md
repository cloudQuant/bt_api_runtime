# bt-api-run-contract

This directory is an independent build root for the `bt_api_run_contract`
package. It has two explicitly versioned YAML schemas and depends at runtime only on
PyYAML and the Python standard library. Package metadata is version `0.1.2` for
this source candidate; this change is not a release.

The 0.1.2 wheel was built and tested from separate target directories on
Python 3.8 and 3.11 (47 tests each). The target checks used the existing
interpreter environments and are not isolated dependency or release validation.
Wheel SHA-256: `4cda8c352accda36321207c0c7bc642d9d80c105e1cedf4da9a4f6d5a5f4b27c`.

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

## Schema version 2 source candidate

`parse_config` is the single parser owner and dispatches on the explicit
`schema_version`. `SCHEMA_VERSION` remains `1` for compatibility;
`SCHEMA_V2_VERSION` is `2`. Version 1 keeps its existing parser result,
reference model, plugin behavior, and resolver tuple.

Version 2 returns a separate frozen `ValidatedRunConfigV2` DTO. Its
`ReferencesV2` has nine field-typed symbolic identifiers: the existing
`strategy`, `provider`, `endpoint`, `account`, `credential`, and `scope`, plus
`product`, `data_source`, and `clock`.

```yaml
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
```

Paper and live require the matching `environment`. The parser recognizes
`replay` and `shadow`, then rejects them with `mode_deferred`; those modes do
not produce a DTO. `resolve_plan` also rejects every v2 DTO with
`schema_v2_resolution_deferred`. Version 2 does not extend or alter the
accepted version-1 `ResolvedPlan` tuple, and the static capability requests
cannot authorize an operation.

`plugins` is required and retains at most 64 named boolean switches with the
version-1 plugin-name grammar, including the gateway, risk, monitor, execution,
and runtime component names used by the candidate. Enabled switches are
preserved in the DTO; parsing does not load, resolve, or authorize a plugin.

`strategy_parameters` is a mapping with at most 64 Python identifier names of
at most 64 characters and 128 UTF-8 bytes (including normal mixed-case
identifiers; Python keywords are rejected).
Values may be YAML null, booleans, signed 32-bit integers, finite floats with
absolute value at most `1e12`, strings of at most 256 characters and 512 UTF-8
bytes, lists, or string-keyed mappings. Strings may be empty; control
characters are rejected. Each nested list or mapping has at most 64 entries,
the value tree is at most 8 levels deep and contains at most 512 scalar and
container nodes, and nested keys use the same string length/byte bounds. The
parser converts lists and maps to the immutable `StrategyParameterList` and
`StrategyParameterMap` wrappers, sorts mapping entries, and rejects non-finite
floats or other Python objects such as dates. These structural bounds do not
replace validation by a strategy's parameter descriptor. The DTO and nested
wrappers omit parameter values from their representations.

Call `ValidatedRunConfigV2.strategy_parameters_dict()` or a nested wrapper's
`to_python()` for fresh ordinary dictionaries and lists. No v2 runtime adapter
is implemented; the deferred resolver and strategy assembly must lower these
copies through a reviewed descriptor path instead of passing immutable wrapper
objects to a strategy.

`requested_capabilities` is a unique list of at most four values from
`market.read`, `account.read`, `orders.paper.write`, and
`orders.live.write`. A paper config cannot request live writes; a live config
cannot request paper writes. This field records a request only. It does not
grant permission, and no version-2 resolver or execution path is implemented.

Version 2 uses the same UTF-8 input bound (64 KiB), YAML event bound (8,192),
and collection-depth bound (32) as version 1. Duplicate keys, aliases,
anchors, tags, multiple documents, and unknown fields remain rejected. The
parser consumes only caller-provided text or bytes; it does not read files,
environment variables, or network services and does not import or execute a
strategy, provider, plugin, or factory. V1 plugin fields and old schema-v4 or
local-simulation fields are not migrated into version 2.

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
