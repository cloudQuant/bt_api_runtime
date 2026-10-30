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
factory actually uses. The application-supplied factory is a deployment trust
boundary and must return a fresh Backtrader `ServiceReadProxy`; the proxy closes
its injected account and market facades, while the application remains
responsible for any shared SDK runtime. This repository does not yet provide a
concrete SDK-to-service-facade factory. Backtrader's current Store startup also
requires a complete read-only account snapshot to initialize its broker, so this
candidate's live path includes read-only account observations as well as bars.

The runtime candidate is not a permission system, provider identity check, live
account acceptance, or complete I52 A1 acceptance. Its local tests fake only the
outer account and market I/O clients while exercising the real proxy, Store,
Feed, Broker, and Cerebro. `replay`, `shadow`, plugin execution, local paper
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
