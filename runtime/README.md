# `bt-api-runtime`

This separately defined build root contains the first I52 A1 runtime assembly
candidate. A wheel was built (SHA-256
`f06c318e65efd756d8ae38b3fd573fd94f1e13220c0bf5d2c147829fe9c248a8`) and installed to a
separate target directory using checkout dependencies. The wheel, install target,
and source `__init__.py` and `assembly.py` payloads matched byte-for-byte.
It accepts only `paper` and `live` configurations resolved through
`bt_api_run_contract`, then assembles one ordinary Backtrader engine with
registered `BtApiFeed` minute bars, `BtApiStore`, and a `BtApiBroker` configured
with `market_data_only=True`.

The application must construct a code-owned `RuntimeBindings` registry. Each
route binds an ordinary `backtrader.Strategy` subclass, a fixed set of `1m` feed
names, and a zero-argument provider factory. The factory must return a fresh
Backtrader `ServiceReadProxy` whose account and market clients are already bound
by application code. Configuration YAML contains only symbolic selectors; it
cannot import strategy or provider modules or supply feed parameters. There is
no default SDK provider factory in this package. Runtime assembly requires a
Backtrader build that exposes the public `ServiceReadProxy.supports_bar_history`
capability API (available in source commit `0f88508d3`). The upstream distribution
version remains `1.4.0` on both older and updated sources, so the package version
constraint alone cannot distinguish this API; assembly checks for it before
calling the provider factory. A provider factory owns all
resources until it returns the exact supported proxy; if it raises or returns
an unsupported object, it must clean its own unhanded resources. The runtime
never calls `close()` on an unknown factory result.

The factory is the deployment trust boundary. A route key or `paper`/`live`
label does not attest actual endpoint or account identity, credential use,
permission, or provider behavior. `ServiceReadProxy` closes its injected account
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

The intended source-checkout sequence is to install `bt_api_run_contract`,
`bt_api_base`, and the required Backtrader build in the environment, then install
this runtime directory. The focused source suite passed 25 tests with one
upstream deprecation warning, using checkout source paths for dependencies.
These checks did not use an isolated virtual environment or verify full
dependency resolution; they are not release or deployment acceptance.
