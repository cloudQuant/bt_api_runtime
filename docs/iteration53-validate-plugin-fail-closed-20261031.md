# 迭代 53：插件启用时 validate fail-closed 修复记录

日期：2026-10-31

## 仓库与基线

- 仓库：`D:\bt_api_py\bt_api\bt_api_runtime`
- 分支：`dev`
- 本轮基线 HEAD：`bb92ce553c11fdb17a058a9810ac6626ef09fb88`
- 基线相对 `origin/dev`：ahead 4。
- 修改前，目标 `runtime/src/bt_api_runtime/cli.py` 和 `runtime/tests/test_cli.py` 均无本地改动。仓库中已有的 `build/`、`*.egg-info/`、`__pycache__/` 未跟踪产物保留。

## 问题与修复

`doctor` 和 `DirectBacktraderSession` 已将启用但当前不支持组合的插件明确标为 `BLOCKED` / `plugin_composition_unavailable`。但 CLI 的 `validate` 调用 `_checks(..., include_plugins=False)`，遗漏插件准入检查；即使配置启用了 `risk`，其余字段有效时仍会返回 `PASS`。这与 `runtime/README.md` 中“validate 和 doctor 都报告启用插件为 blocked”的说明不一致。

本轮只做以下代码和测试修改：

- `runtime/src/bt_api_runtime/cli.py`：`validate` 将插件检查纳入结果，插件选择与 `doctor` 一致地 fail-closed。
- `runtime/tests/test_cli.py`：新增 `test_validate_blocks_enabled_plugins`，断言 `risk: true` 返回退出码 2、整体状态 `BLOCKED`，插件检查码为 `plugin_composition_unavailable`，并回显启用项 `risk`。

没有改变插件装配能力、direct Store 路由或订单行为。启用插件的 direct session 仍在构造阶段明确拒绝；本轮只修复离线配置诊断漏检。

## 验收

执行的是无账户、离线 CLI 单元测试：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$env:PYTHONUTF8='1'
$env:PYTHONPATH='D:\bt_api_py\bt_api\bt_api_runtime\runtime\src;D:\bt_api_py\bt_api\bt_api_runtime\contracts\src'
python -m pytest --noconftest -p no:cacheprovider -p no:asyncio runtime\tests\test_cli.py -q
```

结果：`6 passed in 0.22s`，Python / pytest 退出码为 0。测试仅解析临时配置并检查 CLI 输出及惰性导入；未连接网络、provider、交易所或账户，也未执行订单操作。该结果是本地单元测试证据，不是账户、paper 或 live 验收。

`git diff --check` 通过。Git 提示 `cli.py` 的工作副本 LF 在下次 Git 写入时会转换为 CRLF；本轮未修改仓库的换行配置。

## 当前交付状态

本轮代码范围为上述 CLI 与测试两个文件，另附本记录。按要求尚未提交；现有未跟踪构建产物和其他修改均保留。