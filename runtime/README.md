# `bt-api-runtime`

This separately defined build root contains the first I52 A1 runtime assembly
candidate. The earlier 0.1.0 wheel was built (SHA-256
`f06c318e65efd756d8ae38b3fd573fd94f1e13220c0bf5d2c147829fe9c248a8`) and installed to a
separate target directory using checkout dependencies. The wheel, install target,
and source `__init__.py` and `assembly.py` payloads matched byte-for-byte.
The earlier 0.1.1 wheel pins the contract package at 0.1.2; both installed to a
separate target and the 25 assembly tests passed using local Backtrader and SDK
base source dependencies. This is not isolated dependency or release validation.
The 0.1.1 wheel SHA-256 is `591cf599b4df6a93d5992e02144dc3ae3a614eeaccbd08d4c02ff56bd9a86854`.
The earlier 0.1.2 wheel pins contract 0.1.3 and predates v2 assembly. Installed-target checks passed
26 assembly tests using local Backtrader and SDK base source dependencies; this
is not an isolated dependency or release check. Wheel SHA-256:
`c1d38a09acd60e06fa2dc0c42ac371c1f3dbfc7a1d796e926dd92c5c8327478f`.
The subsequent 0.1.3 wheel includes v2 assembly and pins contract 0.1.4.
Installed-target checks passed 38 tests with one Quandl warning using local
Backtrader and SDK base source dependencies; this is not an isolated dependency
or release check. Wheel SHA-256:
`fb9aade281c3442e8c9c343f320b42314cf9c3f632090864940a8ee861262e31`.
It accepts schema v1 and v2 `paper` and `live` configurations resolved through
`bt_api_run_contract`, then assembles one ordinary Backtrader engine with
registered `BtApiFeed` minute bars, `BtApiStore`, and a `BtApiBroker` configured
with `market_data_only=True`. V1 keeps its existing `RuntimeBindings` behavior.
V2 uses `RuntimeBindingsV2` and binds a route's environment, all nine symbolic
references, static capability declaration, and strategy-parameter contract to
one code-owned strategy class, feed set, and provider factory before invoking
that factory.

The application constructs a code-owned `RuntimeBindings` (v1) or
`RuntimeBindingsV2` (v2) registry. Each route binds an ordinary
`backtrader.Strategy` subclass, a fixed set of `1m` feed names, and a zero-argument
provider factory. The factory must return a fresh Backtrader `ServiceReadProxy`
whose account and market clients are already bound by application code.
Configuration YAML contains symbolic selectors and, for v2, contract-validated
strategy parameters; it cannot import strategy or provider modules or supply
feed configuration. V2 requires an explicit request for both `market.read` and
`account.read`; write requests and enabled plugins fail before the provider
factory runs. V2 parameters are copied to ordinary Python values and passed as
Strategy keyword arguments. There is no default SDK provider factory in this
package. Runtime assembly requires a
Backtrader build that exposes the public `ServiceReadProxy.supports_bar_history`
capability API (available in source commit `0f88508d3`). The upstream distribution
version remains `1.4.0` on both older and updated sources, so the package version
constraint alone cannot distinguish this API; assembly checks for it before
calling the provider factory. A provider factory owns all
resources until it returns the exact supported proxy; if it raises or returns
an unsupported object, it must clean its own unhanded resources. The runtime
never calls `close()` on an unknown factory result.

The factory is the deployment trust boundary. Contract route capabilities,
strategy-parameter descriptors, and a `paper`/`live` label are static declarations;
they do not prove actual provider/account identity, credential use, authorization,
or provider behavior. `ServiceReadProxy` closes its injected account
and market facades when the Store stops; a shared SDK runtime remains owned by
the application. Factories must be failure-atomic and return fresh facades for
each run. Once the exact proxy is returned, the session owns and closes that
proxy and its injected account/market facades; the shared SDK runtime stays
borrowed by the application. Since `BtApiStore.start()` requires a complete
balance snapshot, the read-only account facade is part of startup even though
this candidate's data feeds only carry market bars.

The session is bound to the thread that assembled it. `run()` and `close()`
must use that owner thread because the proxy is thread-bound. Any thread may
call `request_stop()`: it returns `False` until Cerebro has published an active
run, and otherwise only calls Cerebro's thread-safe `runstop()`. The owner thread
performs all cleanup. A live feed with no history and no polled bars does not
exhaust by itself; callers must request a stop or wait for data.

Backtrader does not enter its normal Strategy `_stop()` cleanup when a Strategy
`_start()` fails before its main-loop `try` block. This candidate does not wrap
or replace the registered Strategy class and therefore does not claim to clean
Strategy-owned resources after a failed constructor or `start()`. Keep external
resource acquisition out of Strategy constructors/start hooks, or make the hook
release its own partial resources before raising; full Strategy-start failure
compensation needs an upstream Backtrader lifecycle fix.

The real broker is locked into market-data-only mode, so Backtrader orders are
rejected locally and the proxy exposes no order submission interface. This is a
restricted observation assembly, not a permission system or proof that a live
provider is truly paper/live, and not full A1 acceptance. Local tests fake the
outer account and market clients only. `replay`, `shadow`, plugin execution,
legacy local paper simulation, and order routing are unsupported.

A separate local source-fake test composes the real SDK client runtime, account
and market facades, ServiceRegistry, Backtrader proxy and engine. Only transport,
subscriber and service responses are synthetic. It covers the same strategy in
paper/live, a locally rejected buy with no write RPC, and retention followed by
a retry after an injected facade-close failure. The two focused tests passed
on Python 3.11 using local source dependencies. The test checks timeout values
are passed to the fake transport; it does not verify effective transport timeout
enforcement, real network cleanup, provider identity or account acceptance.

The intended source-checkout sequence is to install `bt_api_run_contract`,
`bt_api_base`, and the required Backtrader build in the environment, then install
this runtime directory. On Python 3.11.5, the focused source suite passed 38
tests with checkout source roots for this runtime, the contract, Backtrader, and
`bt_api_base`; pytest plugin autoload and its cache provider were disabled. The
tests fake the outer account and market clients. These checks did not use an
isolated virtual environment or verify full dependency resolution; they are not
release, deployment, or account acceptance.
