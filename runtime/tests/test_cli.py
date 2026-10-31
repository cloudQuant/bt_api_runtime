"""Focused offline CLI contracts for validate/doctor."""

from __future__ import annotations

import json
import subprocess
import sys

from bt_api_runtime import cli


def _config(tmp_path, *, mode="paper", plugins="  risk: false", duplicate=False):
    text = f"""schema_version: 2
mode: {mode}
environment: {mode}
references:
  strategy: strategy/test
  provider: provider/test
  endpoint: endpoint/test
  account: account/test
  credential: credential/test
  scope: scope/test
  product: product/test
  data_source: data_source/test
  clock: clock/test
plugins:
{plugins}
strategy_parameters:
  window: 20
requested_capabilities:
  - account.read
  - market.read
  - orders.paper.write
"""
    if duplicate:
        text += "mode: paper\n"
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _json(capsys):
    return json.loads(capsys.readouterr().out)


def test_validate_accepts_a_paper_plan_without_loading_backtrader(tmp_path, capsys):
    path = _config(tmp_path)
    assert cli.main(["validate", "--config", str(path)]) == 0
    payload = _json(capsys)
    assert payload["status"] == "PASS"
    assert payload["plan"]["mode"] == "paper"
    assert payload["plan"]["environment"] == "paper"
    assert payload["plan"]["strategy_parameters"] == {"window": "int"}
    assert "window" not in json.dumps(payload["plan"]["references"])
    for check in payload["checks"]:
        assert check["status"] in {"PASS", "NOT_CHECKED"}


def test_validate_rejects_duplicate_and_deferred_mode(tmp_path, capsys):
    assert cli.main(["validate", "--config", str(_config(tmp_path, duplicate=True))]) == 2
    assert _json(capsys)["status"] == "BLOCKED"
    assert cli.main(["validate", "--config", str(_config(tmp_path, mode="replay"))]) == 2
    payload = _json(capsys)
    assert payload["status"] == "BLOCKED"
    assert payload["code"] == "mode_deferred"


def test_validate_blocks_enabled_plugins(tmp_path, capsys):
    path = _config(tmp_path, plugins="  risk: true")

    assert cli.main(["validate", "--config", str(path)]) == 2
    payload = _json(capsys)
    assert payload["status"] == "BLOCKED"
    plugins = next(check for check in payload["checks"] if check["name"] == "plugins")
    assert plugins["status"] == "BLOCKED"
    assert plugins["code"] == "plugin_composition_unavailable"
    assert plugins["enabled"] == ["risk"]


def test_doctor_reports_offline_pass_with_not_checked_items(tmp_path, capsys):
    path = _config(tmp_path)
    assert cli.main(["doctor", "--config", str(path)]) == 0
    payload = _json(capsys)
    assert payload["status"] == "PASS_WITH_NOT_CHECKED"
    assert set(payload["not_checked"]) == {"topology", "trusted_registry", "credentials", "network"}


def test_doctor_blocks_enabled_plugins(tmp_path, capsys):
    path = _config(tmp_path, plugins="  risk: true")
    assert cli.main(["doctor", "--config", str(path)]) == 2
    payload = _json(capsys)
    plugins = next(check for check in payload["checks"] if check["name"] == "plugins")
    assert plugins["status"] == "BLOCKED"
    assert plugins["enabled"] == ["risk"]
    assert "assembly" not in payload


def test_cli_import_is_lazy():
    code = (
        "import sys\n"
        "import bt_api_runtime.cli\n"
        "print('backtrader' in sys.modules, 'bt_api_py' in sys.modules)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)
    assert result.stdout.strip() == "False False"
