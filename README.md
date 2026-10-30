# bt_api_runtime

## System role and authority boundary

The independently built [`contracts/`](contracts/README.md) package provides a
pure-data configuration parser and symbolic route matcher for the initial I52
schema. Matching uses a caller-supplied registry; it does not establish actual
resource identity or grant permissions. Strategy preflight and session orchestration
remain a proposal: this repository does not yet perform runtime preflight,
enable plugins, orchestrate sessions, or grant trading authorization. Existing
runtime behavior remains in Backtrader until a separately verified migration.
This repository is not an authority for readiness or permission to trade. See the
[Trading System Architecture](https://github.com/cloudQuant/backtrader/blob/dev/docs/TRADING_SYSTEM_ARCHITECTURE.md).

The `dev` branch is the development entry. This repository is included as a
submodule of [bt_api_py](https://github.com/cloudQuant/bt_api_py).
