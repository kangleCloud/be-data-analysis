#!/usr/bin/env bash
# 仅在目标Linux主机由管理员执行：安装服务，不启动业务或覆盖生产配置。
set -euo pipefail

APP_DIR=/data/content/be-data-analysis
LOG_DIR=/data/logs/be-data-analysis
RUN_USER=${RUN_USER:-be-data-analysis}
PYTHON_BIN=${PYTHON_BIN:-python3.12}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

if [[ ${EUID} -ne 0 ]]; then
    echo '请在目标主机使用sudo执行安装脚本。' >&2
    exit 1
fi
# 从管理员的PATH解析已有解释器，不限制Linux发行版。
PYTHON_BIN=$(command -v "$PYTHON_BIN") || {
    echo '请将python3.12加入PATH，或通过PYTHON_BIN指定解释器。' >&2
    exit 1
}
if [[ ! $RUN_USER =~ ^[a-z_][a-z0-9_-]*[$]?$ ]]; then
    echo 'RUN_USER必须是有效的Linux账号名称。' >&2
    exit 1
fi
"$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)'
for required in systemctl systemd-analyze logrotate runuser useradd groupadd; do
    command -v "$required" >/dev/null
done
if [[ ! -f $APP_DIR/requirements.txt || ! -d $APP_DIR/app || ! -f $APP_DIR/.env.prod ]]; then
    echo '先部署源代码和私有.env.prod到/data/content/be-data-analysis。' >&2
    exit 1
fi
if systemctl is-active --quiet be-data-analysis.service; then
    echo '请先停止原生服务，再安装或更新。' >&2
    exit 1
fi
if [[ -e $APP_DIR/.venv || -L $APP_DIR/.venv ]]; then
    echo '请先备份目标主机旧.venv；本脚本只创建全新虚拟环境，不复用或删除旧环境。' >&2
    exit 1
fi
# 已有账号（包括root）沿用主组；默认专用账号不存在时才创建。
if ! id "$RUN_USER" >/dev/null 2>&1; then
    if ! getent group "$RUN_USER" >/dev/null; then
        groupadd --system "$RUN_USER"
    fi
    useradd --system --gid "$RUN_USER" --home-dir "$APP_DIR" --no-create-home \
        --shell /usr/sbin/nologin "$RUN_USER"
fi
RUN_GROUP=$(id -gn "$RUN_USER")
if [[ ! $RUN_GROUP =~ ^[a-z_][a-z0-9_-]*[$]?$ ]]; then
    echo '运行账号的主组名称无效。' >&2
    exit 1
fi
# 配置仅调整访问权限，绝不复制样例覆盖已有私有值。
chown root:"$RUN_GROUP" "$APP_DIR/.env.prod"
chmod 0640 "$APP_DIR/.env.prod"
runuser -u "$RUN_USER" -- test -r "$APP_DIR/app/__main__.py"
install -d -o "$RUN_USER" -g "$RUN_GROUP" -m 0750 "$APP_DIR/.venv" "$LOG_DIR"
# touch仅创建不存在的日志；已有内容保留，归档的权限随原生运行账号统一。
touch "$LOG_DIR/service.log"
find "$LOG_DIR" -maxdepth 1 -type f -name 'service.log*' \
    -exec chown "$RUN_USER:$RUN_GROUP" {} + -exec chmod 0640 {} +
runuser -u "$RUN_USER" -- "$PYTHON_BIN" -m venv "$APP_DIR/.venv"
runuser -u "$RUN_USER" -- env PYTHONDONTWRITEBYTECODE=1 \
    "$APP_DIR/.venv/bin/pip3.12" install --no-cache-dir -r "$APP_DIR/requirements.txt"
runuser -u "$RUN_USER" -- test -r "$APP_DIR/.env.prod"
# 将选定账号同时写入服务和日志轮转配置，避免两者权限不一致。
GENERATED_DIR=$(mktemp -d)
trap 'rm -rf -- "$GENERATED_DIR"' EXIT
sed -e "s/^User=.*/User=$RUN_USER/" -e "s/^Group=.*/Group=$RUN_GROUP/" \
    "$SCRIPT_DIR/be-data-analysis.service" > "$GENERATED_DIR/be-data-analysis.service"
sed "s/    su .*/    su $RUN_USER $RUN_GROUP/" \
    "$SCRIPT_DIR/logrotate.conf" > "$GENERATED_DIR/logrotate.conf"
systemd-analyze verify "$GENERATED_DIR/be-data-analysis.service"
logrotate --debug "$GENERATED_DIR/logrotate.conf"
install -m 0644 "$GENERATED_DIR/be-data-analysis.service" /etc/systemd/system/be-data-analysis.service
# Docker与原生使用同一个日志文件，因此只安装其中一种轮转配置。
install -m 0644 "$GENERATED_DIR/logrotate.conf" /etc/logrotate.d/be-data-analysis
systemctl daemon-reload
systemctl enable be-data-analysis.service
printf '%s\n' '安装完成，尚未启动。请核对虚拟IP和网络配置，停止旧容器后再systemctl start be-data-analysis。'
