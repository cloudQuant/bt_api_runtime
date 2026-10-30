# bt_api_runtime

Proposed optional runtime and preflight component for the `bt_api_py` ecosystem.
Its intended scope includes strategy and configuration checks before a live
session; its interfaces and migration boundary are still to be designed.

## System role and authority boundary

This repository is a placeholder for optional runtime and preflight work; it
contains no implementation. It currently performs no preflight checks, plugin
enablement, session orchestration, or trading authorization. Any runtime
behavior already implemented remains in Backtrader until a separately verified
migration. This checkout is not an authority for readiness or permission to
trade. See the
[Trading System Architecture](https://github.com/cloudQuant/backtrader/blob/dev/docs/TRADING_SYSTEM_ARCHITECTURE.md).

This repository is an initial placeholder. It does not yet provide a runtime,
perform checks, enable plugins, or authorize trading. Existing
`backtrader_runtime` behavior remains in Backtrader until separately migrated
and verified.

The `dev` branch is the development entry. This repository is included as a
submodule of [bt_api_py](https://github.com/cloudQuant/bt_api_py).
