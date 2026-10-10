"""静态说明命令与实际源调用契约的离线回归。"""

import ast
import builtins
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest
from app import cli

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("as_json", [False, True])
def test_sources_does_not_read_configuration_import_runtime_or_access_network(monkeypatch, capsys, as_json):
    original_import = builtins.__import__
    forbidden = {"redis", "uvicorn", "akshare", "pandas", "dotenv", "pydantic", "fastapi"}
    def restricted_import(name, *args, **kwargs):
        assert name.split(".")[0] not in forbidden
        assert name not in {"app.core.config", "app.core.logging", "app.runtime.workflows"}
        return original_import(name, *args, **kwargs)
    def forbidden_io(*args, **kwargs):
        pytest.fail("sources 不得读配置文件或访问网络")
    monkeypatch.setattr(builtins, "__import__", restricted_import)
    monkeypatch.setattr(builtins, "open", forbidden_io)
    monkeypatch.setattr(Path, "open", forbidden_io)
    monkeypatch.setattr(socket.socket, "connect", forbidden_io)
    monkeypatch.setenv("APP_ENV", "invalid-private-environment")
    monkeypatch.setattr(sys, "argv", ["app", "sources"] + (["--json"] if as_json else []))
    assert cli.main() == 0
    output = capsys.readouterr()
    assert not output.err
    if as_json:
        assert json.loads(output.out)["verificationStatus"] == "NOT_CHECKED"
    else:
        assert "13 个唯一函数，14 种参数调用组合" in output.out
        assert "不证明任何源当前健康" in output.out


def test_sources_runs_without_site_packages_and_private_values_do_not_leak(tmp_path):
    env = os.environ.copy()
    env.update(PYTHONPATH=str(ROOT), APP_ENV="invalid-environment",
               REDIS_URL="private-redis-host", REDIS_PASSWORD="private-redis-password",
               XUEQIU_TOKEN="private-xueqiu-token", STOCK_MONITOR_INTERNAL_TOKEN="private-internal-token",
               SOURCE_TIMEOUT_SECONDS="invalid-value")
    (tmp_path / ".env.dev").write_text("REDIS_PASSWORD=private-file-password\n", encoding="utf-8")
    result = subprocess.run([sys.executable, "-S", "-m", "app", "sources", "--json"],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and not result.stderr
    payload = json.loads(result.stdout)
    assert payload["verificationStatus"] == "NOT_CHECKED"
    for value in ("private-redis-host", "private-redis-password", "private-xueqiu-token",
                  "private-internal-token", "private-file-password"):
        assert value not in result.stdout


def test_json_catalog_covers_current_provider_functions_and_literal_parameter_combinations():
    payload = json.loads(subprocess.run(
        [sys.executable, "-S", "-m", "app", "sources", "--json"], cwd=ROOT,
        check=True, capture_output=True, text=True, timeout=5,
    ).stdout)
    provider_files = [*sorted(path for path in (ROOT / "app/providers").glob("*.py") if path.name != "catalog.py"), ROOT / "app/calendar/service.py"]
    functions = set()
    fixed_combinations = set()
    for path in provider_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bindings = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr.startswith(("stock_", "fund_", "tool_trade_")):
                functions.add(node.attr)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith(("stock_", "fund_", "tool_trade_")):
                functions.add(node.value)
            if isinstance(node, ast.Assign):
                targets = {child.attr for child in ast.walk(node.value) if isinstance(child, ast.Attribute)
                           and child.attr.startswith(("stock_", "fund_", "tool_trade_"))}
                for target in node.targets:
                    if isinstance(target, ast.Name) and targets:
                        bindings[target.id] = targets
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                targets = {node.func.attr} & functions
                if node.args:
                    argument = node.args[0]
                    if isinstance(argument, ast.Attribute):
                        targets |= {argument.attr} & functions
                    elif isinstance(argument, ast.Name):
                        targets |= bindings.get(argument.id, set())
                for keyword in node.keywords:
                    if keyword.arg == "symbol" and isinstance(keyword.value, ast.Constant):
                        fixed_combinations.update((target, keyword.value.value) for target in targets)
    sources = payload["sources"]
    assert {entry["function"] for entry in sources} == functions
    combinations = {(entry["function"], json.dumps(entry["parameters"], sort_keys=True)) for entry in sources}
    assert len(combinations) == len(sources) == payload["parameterCombinationCount"]
    assert payload["uniqueFunctionCount"] == len(functions)
    assert fixed_combinations <= {(entry["function"], entry["parameters"].get("symbol")) for entry in sources}
    # 同函数主板/科创板的两种已实施调用必须单独计数，ETF字典/行情共用定义。
    assert len(sources) == len(functions) + 1
    etf_entries = [entry for entry in sources if entry["function"] == "fund_etf_category_sina"]
    assert len(etf_entries) == 1 and len(etf_entries[0]["uses"]) == 2
    for entry in sources:
        assert all(entry[field] for field in (
            "uses", "provider", "endpoints", "authorization", "frequency", "timeout",
            "availability", "semantics", "entrypoints",
        ))
        assert all(endpoint["method"] in {"GET", "POST"} and endpoint["domain"]
                   and endpoint["path"].startswith("/") for endpoint in entry["endpoints"])
