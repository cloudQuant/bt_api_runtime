# bt_api_runtime

## System role and authority boundary

The independently built [`contracts/`](contracts/README.md) package provides a
pure-data parser and symbolic route matcher for the initial I52 schema. The new
[`runtime/`](runtime/README.md) build root adds a first constrained assembly
candidate for registered `paper` and `live` read-only bar observation. It binds
strategy classes, minute-feed declarations, and a provider factory in application
code; YAML can only select one complete existing binding. It uses Backtrader's
ordinary `Cerebro`, `BtApiFeed`, `BtApiStore`, and `BtApiBroker`, with the broker's
`market_data_only` guard. It does not add another scheduler, permit orders, or
fall back to the legacy `paper` local simulation.

Route identifiers and environment labels are only configuration selectors. They
do not prove which endpoint, account, credentials, provider, or permissions a
factory actually uses. Generic application-supplied factories remain a
deployment trust boundary and must return a fresh Backtrader `ServiceReadProxy`;
the application owns any SDK runtime shared by those factories. The runtime also
provides an optional, code-owned `SdkReadOnlyFacadeFactory` for one v2 route.
Every schema-v2 provider factory must expose a callable
`preflight(selected_route)` hook returning exactly
`True`; missing/non-callable hooks, rejected values, and ordinary lookup/call
exceptions fail closed before invoking that factory. Schema v1 keeps its prior
optional-hook behavior and accepts a legacy factory without one. Preflight is
only a consistency check, not identity or permission proof. The resource owner
retains the SDK runtime, account and market facades, and proxy;
after the session closes the per-run proxy, the application calls
`factory.owner.close()` to finish that owner's cleanup.

Current verification of this SDK composition is local source-fake only: it
exercises the real SDK runtime and facades with synthetic transport, subscriber,
and service-response leaves. Route preflight proves only configuration
consistency, and the owner tests cover local resource lifecycle behavior. Actual
provider/source identity, source-descriptor truth, clock calibration, account
authorization, and real-account acceptance remain **NOT_PROVEN**. Backtrader's
current Store startup also requires a complete read-only account snapshot to
initialize its broker, so this candidate's live path includes read-only account
observations as well as bars.

The runtime candidate is not a permission system, provider identity check, live
account acceptance, or complete I52 A1 acceptance. The focused SDK source-fake
test exercises the real SDK runtime, account and market facades, ServiceRegistry,
Backtrader proxy, and engine; only the transport, subscriber, and service-response
leaves are synthetic. `replay`, `shadow`, plugin execution, local paper
simulation, and order routing are unsupported. See the [Trading System
Architecture](https://github.com/cloudQuant/backtrader/blob/dev/docs/TRADING_SYSTEM_ARCHITECTURE.md).

## Build roots

`contracts/` and `runtime/` have separate Python build roots and PEP 517
metadata. A `bt-api-runtime` wheel was built (SHA-256
`f06c318e65efd756d8ae38b3fd573fd94f1e13220c0bf5d2c147829fe9c248a8`) and installed
to a separate target directory using current checkout dependencies. Its two Python
payload files matched the source, wheel, and install target byte-for-byte. The
25 focused runtime tests passed with one upstream deprecation warning, using
checkout source paths for dependencies. This was not an isolated virtual
environment, full dependency-resolution check, or release acceptance. Runtime
package metadata is defined in [`runtime/pyproject.toml`](runtime/pyproject.toml).

The `dev` branch is the development entry. This repository is included as a
submodule of [bt_api_py](https://github.com/cloudQuant/bt_api_py).
