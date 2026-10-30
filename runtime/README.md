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

The current source candidate is version 0.1.11 and pins the contract package
at `bt-api-run-contract==0.1.5`. It includes an optional
lazy-imported SDK read-only facade factory and resource owner; the Python 3.11+
SDK remains absent from the runtime's required dependencies so ordinary
package imports continue to support Python 3.9+.

The application constructs a code-owned `RuntimeBindings` (v1) or
`RuntimeBindingsV2` (v2) registry. Each route binds an ordinary
`backtrader.Strategy` subclass, a fixed set of `1m` feed names, and a zero-argument
provider factory. The factory must return a fresh Backtrader `ServiceReadProxy`
whose account and market clients are already bound by application code.
Configuration YAML contains symbolic selectors and, for v2, contract-validated
strategy parameters; it cannot import strategy or provider modules or supply
feed configuration. V2 requires an explicit request for both `market.read` and
`account.read`; write requests and enabled plugins fail before the provider
factory runs. Every V2 provider factory must expose a callable
`preflight(selected_route, *, run_id)` hook that returns the singleton `True`;
`assemble_run(source, bindings, run_id=...)` requires the caller to supply a
run identity for V2. The accepted syntax is 1–128 ASCII characters matching
`[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`. Missing or malformed values fail before
factory preflight. An empty value is rejected before the SDK runtime constructor,
so this integration does not rely on the SDK's empty-value UUID fallback. V1
keeps its previous call shape and has no run identity.
A missing or non-callable hook, a non-`True` result, or an ordinary lookup/call
exception fails closed before the provider factory is invoked. V1 retains its
prior zero-argument factory behavior, including support for factories without
a preflight hook. V2 parameters are copied to ordinary Python values and passed
as Strategy keyword arguments. `RunSession.run_id` exposes the same explicit
V2 identifier. There is no default SDK provider factory in
this package. Runtime assembly requires a
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

## Code-owned SDK read-only factory and resource owner

`SdkReadOnlyFacadeFactory` is an optional, lazy-imported composition for one
v2 route. Construct it with a parsed `bt_api_py.service_config.ClientConfig`,
the exact `TrustedRouteV2`, an `SdkRouteReferencesV2` containing nine explicit
I52-reference-to-SDK-reference pairs, an explicit paper/live environment map,
typed SDK identity/account/instrument/scope objects, endpoint bindings, and
code-owned clock, transport, and subscriber factories. The mapping is checked
against the selected route and the complete public `ClientConfig` projection
before the provider factory runs. It requires exactly the read-only
`store.account` and `store.market` mounts, matching service/instance/scope and
account refs, a single matching communication profile, and no extra mounts.
The route strategy ref is explicitly mapped to SDK `WireIdentity.actor_ref`;
the two code-owned principal refs are also checked, and delegation refs are
rejected. Provider, account, endpoint/transport, credential-profile, scope,
product, data source, and clock mappings each have separate fields. Symbol
equality alone never establishes provider identity or permission.

The SDK clock ref is paired immutably with a code-owned callable through
`SdkClockBinding`. This records the selected mapping only; it does not prove
clock calibration or that provider timestamps are authoritative. Scope checks
bind the account and strategy dimensions, require the market scope's declared
strategy, require a public stream scope, and reject non-empty unknown scope
dimensions. SDK identity and scope checks validate supplied typed objects; they
do not replace the server's authenticated grants or prove the real provider
behind a deployment endpoint.

The code-owned `SdkReadOnlySpecV2` carries an explicit
`SdkMarketSourceExpectation`: `source`, `clock_domain_id`,
`clock_source_session_id`, and `clock_source`. The factory passes those
independent values to the SDK's `ExpectedMarketSourceIdentity`; missing or
invalid values fail preflight before a transport or subscriber is created.
The SDK checks all four fields on the initial subscribe and each recovery
subscribe, so a mismatch blocks bootstrap or recovery data delivery. This
runtime binds one market client and uses the SDK's singleton `start()` path.
The SDK's separate `start_many()` batch path requires the batch bootstrap
operation when an expected identity is present; it does not fall back to
singleton starts if that operation is unavailable. This runtime adds no batch
scheduler.

Lazy preflight checks that the SDK exports both `ExpectedMarketSourceIdentity`
and `ServiceMarketClient`, and that the market-client constructor explicitly
accepts `expected_source_identity`. Missing SDK modules or import failures
produce the finite `sdk_read_dependencies_unavailable` code; an importable SDK
without this API produces `sdk_market_source_identity_unsupported`. The
constructor check is structural compatibility evidence only: it does not
prove that an SDK implementation enforces the expectation, that a descriptor
is authentic, or that a provider or plugin has the claimed identity.

These checks establish local consistency between code-owned expectations and a
service-supplied typed descriptor only. They do not prove descriptor truth,
provider/source authenticity, account authorization, or clock calibration;
those remain **NOT_PROVEN**. The local source-fake tests exercise the typed
descriptor and recovery checks with synthetic values, not an actual provider
source.

The factory builds the real `ServiceClientRuntime`, `ServiceAccountClient`,
`ServiceMarketClient`, and Backtrader `ServiceReadProxy`. Only the SDK's outer
request/reply transport and event subscriber leaves are injectable, so source
tests can use deterministic fakes without replacing the runtime or facades.
SDK imports remain lazy: importing `bt_api_runtime` and using ordinary runtime
assembly do not load `bt_api_py`. The SDK-backed factory loads SDK modules in
`preflight(selected_route, *, run_id)`; a direct `build(run_id=...)` performs
that preflight first when needed. A prior explicit preflight also binds the
run ID used by a subsequent zero-argument factory call. The factory passes the
bound value to `ServiceClientRuntime(run_id=...)`, then checks the actual
runtime ID and both immutable account/market registration scopes and operation
sets using the SDK's public binding views. It separately checks that the
public stream scope still has no run ID.
This keeps the Python 3.11+ SDK optional for ordinary Python 3.9+ runtime
imports and v1 assembly.

`RunSession` drives the per-run proxy close and borrows the factory-owned SDK
runtime. The application keeps `factory.owner`, lets the session finish its
proxy close, then calls `factory.owner.close()` on the same owner thread. The
resource owner closes the proxy/facades first, then closes the SDK runtime only
after the facade call returns. On any failure it keeps the entire visible
resource group for an owner-thread retry. It checks SDK `state`,
`cleanup_failures`, `unregister_uncertainties`, and `unknown_registration_count`;
`is_connected == False` or runtime `state == "closed"` alone is never treated
as complete cleanup. An uncertain remote registration remains unconfirmed, and
a later retry is not promised to repair it. The SDK has no general standalone
close-receipt DTO, so these observations are derived from normal facade/runtime
returns and the SDK's public uncertainty counters, not independent remote proof.

The SDK factory exposes the required public
`preflight(selected_route, *, run_id)` hook, which `assemble_run` calls after
selecting the code-owned route and before calling the factory. V2 factories
without a callable hook, or whose hook does not return exactly `True`, fail
closed before provider construction. V1 keeps its prior optional-hook
behavior. Preflight binds local composition inputs only. A matching string or
UUID does not establish uniqueness, endpoint identity, authorization, source
truth, or account acceptance; a trusted registry or deployment authority is
still needed for provider/account identity and permission decisions.

The session is bound to the thread that assembled it. `run()` and `close()`
must use that owner thread because the proxy is thread-bound. Any thread may
call `request_stop()`: it returns `False` until Cerebro has published an active
run, and otherwise only calls Cerebro's thread-safe `runstop()`. The owner thread
performs all cleanup. A live feed with no history and no polled bars does not
exhaust by itself; callers must request a stop or wait for data.

When startup, `run()`, or a `with` body already has a primary exception, a
cleanup failure does not replace it. `RunSession.cleanup_report` (or the
primary exception's `runtime_cleanup_report`) exposes a finite scope, required
anonymous action ids, and fixed step/action outcomes, never exception text,
feed names, or configuration values. An empty report or an unobserved required
action is incomplete. A successful normal `run()` report has scope
`run_session` and covers only the Store stopped state and the proxy disconnected
state observed by this session. Cerebro owns normal Broker and Feed teardown;
this report does not claim per-Broker, per-Feed, Strategy, or whole-Cerebro
closure because this session receives no per-component normal-stop receipts.
If this session itself performs compensation, each exact `BtApiFeed` stop has
an anonymous stable id such as `feed.stop/0`; unsupported component types or
unreadable feed state remain unknown/pending rather than being treated as
inactive. `complete` means every required action in the declared scope was
confirmed; it is a structural calculation over the report's supplied fields.
`RuntimeCleanupReport` is a public value object callers can construct manually:
scope, action ids, and statuses are not authenticated from source execution and
do not prove account identity, permission, or authority. `unknown_actions` is a
separate unresolved category; it need not also appear in `pending_actions`, but
always prevents `complete`. A failed initial cleanup call may be followed by a
confirmed owner-thread compensation retry, so the latest session report can be
complete while the surfaced error retains its immutable earlier report.
`GeneratorExit`,
`KeyboardInterrupt`, and `SystemExit` from cleanup propagate when there is no
earlier primary failure; otherwise the earlier failure remains authoritative.
A partial `assemble_run`
has no session object, so it attaches the report to the primary exception only
when that exception exposes a writable instance dictionary with no conflicting
`runtime_cleanup_report` attribute. If attachment is unsupported, the original
exception is still preserved and no report is claimed to be observable. With
an existing body exception, `__exit__` does not
repeat cleanup already attempted by the session; an owner may explicitly retry
`close()` afterward when the session remains open. Before reusing a confirmed
Broker stop, a retry re-reads the exact `BtApiBroker` `_live_started` boolean:
`False` avoids a duplicate stop, while `True` triggers another owner-thread stop
and must be observed `False` afterward. An unreadable or non-boolean state is
unknown and incomplete. This private flag is only the Broker's current
live-start observation, not a lifecycle generation or proof that no failed
start was attempted. `BtApiBroker.start()` can reset it after hydration fails,
while the Store remains connected; Store stopped state, proxy terminal state,
and each required Feed action therefore remain separate obligations before
this report can be complete. The Broker action does not prove account state or
whole-Cerebro closure. If the Broker stop itself did not return, it remains
retryable; a Store shutdown performed earlier by Cerebro but not observed in a
session cleanup report is not treated as confirmed here.

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
provider is truly paper/live, and not full A1 acceptance. The SDK-backed
source-fake path below uses the real client runtime and facades with synthetic
transport, subscriber, and service response leaves. `replay`, `shadow`, plugin
execution, legacy local paper simulation, and order routing are unsupported.

A separate local source-fake test composes the real SDK client runtime, account
and market facades, ServiceRegistry, Backtrader proxy and engine through the
code-owned factory and resource owner. Only the transport, subscriber and
service response leaves are synthetic. An injected transport factory must
release any partial allocation itself if it raises before returning a transport,
because the SDK runtime cannot own a resource it never received. The test covers
the same strategy in paper/live, a locally rejected buy with no write RPC,
pre-I/O rejection for mismatched route/environment/clock/scope maps, retention
and retry after a subscriber-close failure, and an unregister response loss
that remains unconfirmed after retry. These tests do not verify
effective transport timeout enforcement, real network cleanup, provider
identity, source-descriptor truth, authorization, clock calibration, or account
acceptance. Generic v2 factories need the same callable preflight contract, but
their hook is only a local consistency check; selected-route correctness does
not prove identity, authorization, or permission. The SDK source-fake path
continues to exercise the real SDK factory and its real route-mapping checks.

The intended source-checkout sequence is to install `bt_api_run_contract`,
`bt_api_base`, and the required Backtrader build in the environment, then install
this runtime directory. The focused owner/factory source-fake file passed 12
tests on Python 3.11.5 with checkout source roots for this runtime, the
contract, Backtrader, and `bt_api_base`; pytest plugin autoload and its cache
provider were disabled. One existing Backtrader Quandl deprecation warning was
reported. The tests use the real SDK runtime/account/market facades and fake
only the outer transport, subscriber, and service response leaves. These checks
did not use an isolated virtual environment or verify full dependency
resolution; they are not release, deployment, or account acceptance.
