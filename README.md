# bt_api_runtime

Proposed optional runtime and preflight component for the `bt_api_py` ecosystem.
Its intended scope includes strategy and configuration checks before a live
session; its interfaces and migration boundary are still to be designed.

This repository is an initial placeholder. It does not yet provide a runtime,
perform checks, enable plugins, or authorize trading. Existing
`backtrader_runtime` behavior remains in Backtrader until separately migrated
and verified.

The `dev` branch is the development entry. This repository is included as a
submodule of [bt_api_py](https://github.com/cloudQuant/bt_api_py).
