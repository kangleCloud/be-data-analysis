"""原生部署的单进程、权限、日志和停止约束静态验证，不执行安装。"""

from pathlib import Path
import subprocess
import sys

from app.core.config import load_settings

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy/systemd"


def unit():
    sections = {}
    current = None
    for line in (DEPLOY / "be-data-analysis.service").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections[current] = {}
        else:
            key, value = line.split("=", 1)
            sections[current].setdefault(key, []).append(value)
    return sections


def test_native_unit_uses_single_nonroot_prod_service_and_existing_logs():
    sections = unit()
    service = sections["Service"]
    assert service["User"] == service["Group"] == ["be-data-analysis"]
    assert service["WorkingDirectory"] == ["/data/content/be-data-analysis"]
    assert service["ExecStart"] == ["/data/content/be-data-analysis/.venv/bin/python -m app serve"]
    assert not any(key in service for key in ("ExecReload", "EnvironmentFile"))
    assert not any(key.startswith("Memory") for key in service)
    environment = dict(item.split("=", 1) for item in service["Environment"])
    assert environment["APP_ENV"] == "prod" and environment["SERVICE_PORT"] == "18801"
    assert "SERVICE_HOST" not in environment
    assert all(environment[name] == "1" for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"))
    assert service["StandardOutput"] == ["append:/data/logs/be-data-analysis/service.log"]
    assert service["StandardError"] == ["inherit"]
    assert service["Restart"] == ["on-failure"] and service["RestartSec"] == ["5"]
    assert service["KillMode"] == ["mixed"] and service["KillSignal"] == ["SIGTERM"]
    assert service["SendSIGKILL"] == ["yes"] and service["TimeoutStopSec"] == ["60"]
    assert sections["Install"]["WantedBy"] == ["multi-user.target"]


def test_native_install_script_is_valid_and_does_not_start_or_overwrite_config():
    script = DEPLOY / "install.sh"
    subprocess.run(["bash", "-n", str(script)], check=True, capture_output=True, text=True)
    text = script.read_text()
    assert 'systemctl enable be-data-analysis.service' in text
    assert '\nsystemctl start ' not in text
    assert '/etc/os-release' not in text and '.env.prod\n' not in text
    assert '"$APP_DIR/.venv/bin/pip3.12" install --no-cache-dir -r "$APP_DIR/requirements.txt"' in text
    assert 'requirements-dev' not in text
    assert 'touch "$LOG_DIR/service.log"' in text
    assert 'PYTHON_BIN=${PYTHON_BIN:-python3.12}' in text
    assert '-m venv "$APP_DIR/.venv"' in text
    assert 'rm -rf -- "$GENERATED_DIR"' in text and 'truncate' not in text
    assert 'RUN_USER=${RUN_USER:-be-data-analysis}' in text
    assert 'RUN_GROUP=$(id -gn "$RUN_USER")' in text
    assert '运行账号必须非root' not in text
    assert 's/^User=.*/User=$RUN_USER/' in text
    assert 's/^Group=.*/Group=$RUN_GROUP/' in text
    assert 'su $RUN_USER $RUN_GROUP' in text
    assert 'env.prod.example' not in text


def test_native_rotation_matches_account_and_excludes_duplicate_docker_rule():
    rotation = (DEPLOY / "logrotate.conf").read_text()
    assert rotation.count("/data/logs/be-data-analysis/service.log") == 1
    assert 'su be-data-analysis be-data-analysis' in rotation
    assert 'copytruncate' in rotation and 'delaycompress' in rotation
    assert 'postrotate' not in rotation
    # Docker规则保留原容器账号，原生安装仅选择自己的单份规则。
    assert 'su 10001 10001' in (ROOT / "deploy/logrotate.conf").read_text()


def test_serve_has_bounded_shutdown_and_preserves_configured_virtual_ip(monkeypatch):
    from app import cli
    calls = []
    settings = load_settings({"SERVICE_HOST": "python-vip.example", "SERVICE_PORT": "18801"})
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(sys, "argv", ["app", "serve"])
    assert cli.main() == 0
    assert calls[0][0] == ("app.main:app",)
    assert calls[0][1]["host"] == "python-vip.example" and calls[0][1]["port"] == 18801
    assert calls[0][1]["timeout_graceful_shutdown"] == 30
    assert "workers" not in calls[0][1] and "reload" not in calls[0][1]
