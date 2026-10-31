"""Constrained paper/live assembly and lifecycle for Backtrader.

The package root stays lazy: importing :mod:`bt_api_runtime` does not load
Backtrader, SDK service code or provider modules.  Names are resolved on first
access, and the direct Backtrader adapter is a separate optional module.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_MODULE_BY_NAME = {}
for _module_name, _names in (
    (
        ".assembly",
        (
            "FeedSpec",
            "RouteBinding",
            "RouteBindingV2",
            "RunSession",
            "RuntimeAssemblyError",
            "RuntimeBindings",
            "RuntimeBindingsV2",
            "RuntimeCleanupReport",
            "assemble_run",
        ),
    ),
    (
        ".sdk_readonly",
        (
            "SdkClockBinding",
            "SdkEnvironmentMapping",
            "SdkMarketSourceExpectation",
            "SdkReadOnlyError",
            "SdkReadOnlyFacadeFactory",
            "SdkReadOnlyResourceOwner",
            "SdkReadOnlySpecV2",
            "SdkReadOnlyTargetV2",
            "SdkReadOnlyTargetsV2",
            "SdkRouteReference",
            "SdkRouteReferencesV2",
        ),
    ),
    (
        ".direct_backtrader",
        (
            "DirectBacktraderSession",
            "DirectCredentials",
            "DirectProviderProfile",
            "DirectSessionError",
            "DirectSessionReport",
            "DirectWriteGrant",
        ),
    ),
    (
        ".paper_composition",
        (
            "PaperCompositionError",
            "build_grant",
            "load_credentials",
            "summarize",
            "write_evidence",
        ),
    ),
):
    for _name in _names:
        _MODULE_BY_NAME[_name] = _module_name
del _module_name, _names, _name

__all__ = sorted(_MODULE_BY_NAME)


def __getattr__(name: str) -> Any:
    module_name = _MODULE_BY_NAME.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_MODULE_BY_NAME))
