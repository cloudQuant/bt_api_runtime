# bt_api_runtime

## System role and authority boundary

This repository is a placeholder for proposed strategy and configuration
checks before a live session; its interfaces and migration boundary remain to
be designed. It has no implementation: it performs no preflight checks,
enables no plugins, provides no session orchestration, and grants no trading
authorization. Any runtime behavior already implemented remains in Backtrader
until a separately verified migration. This repository is not an authority for
readiness or permission to trade. See the
[Trading System Architecture](https://github.com/cloudQuant/backtrader/blob/dev/docs/TRADING_SYSTEM_ARCHITECTURE.md).

The `dev` branch is the development entry. This repository is included as a
submodule of [bt_api_py](https://github.com/cloudQuant/bt_api_py).
