# Linux systemd 原生部署

本说明纳入[v1.0.0工作树文档归档](../../docs/releases/v1.0.0.md)；公开V1及`schemaVersion=1`保持不变，归档不等同生产发布。

应用目录为`/data/content/be-data-analysis`，使用目标主机已有Python 3.12虚拟环境，原生端口18801。主机需提供systemd和logrotate，不限制Ubuntu版本。服务默认以root运行；其他账号直接修改unit的`User`、`Group`和轮转配置的`su`，三者保持一致，并确保账号可以读取源码、生产配置和虚拟环境、写入日志目录。

## 配置

业务配置统一放在应用目录私有`.env.prod`，参考`deploy/systemd/env.prod.example`，保留已有密码和内部令牌，不用样例覆盖生产文件。本地继续默认读取`.env.dev`。

- `SERVICE_HOST`：实际Python虚拟IP；主机须已配置该IP。
- `SERVICE_PORT=18801`：原生服务监听端口。
- `SERVICE_LOG_LEVEL`：日志级别。
- `REDIS_URL`、`REDIS_PASSWORD`、`REDIS_PORT`、`REDIS_DB`：沿用原Redis虚拟IP、凭据和DB2；原生主机不使用Docker专属的`host.docker.internal`。
- `SOURCE_TIMEOUT_SECONDS`、`REDIS_LOCK_SECONDS`：现有超时和锁配置。
- `STOCK_MONITOR_INTERNAL_TOKEN`：沿用现有内部令牌；启用雪球时再配置`STOCK_MONITOR_XQ_ENABLED=true`及`XUEQIU_TOKEN`。

配置文件路径由代码位置确定：当前部署应为`/data/content/be-data-analysis/.env.prod`，不是运行命令所在目录的任意`.env.prod`。文件缺失不会自动阻止启动，`SERVICE_PORT`缺失时程序默认8000，因此原生部署必须显式设置18801。运行时环境变量优先于文件配置。

systemd仅保留`APP_ENV=prod`选择配置文件，以及`PYTHONUNBUFFERED=1`实时输出日志。端口不在unit中重复设置，不设置数值库线程、TZ或字节码开关。应用日志和源进程使用既有上海时间逻辑。`.env.prod`由应用dotenv加载，不通过shell source或systemd EnvironmentFile解析，密码不展开变量。

Java的admin、scheduler、openapi三处生产地址均使用`http://<PYTHON_VIRTUAL_IP>:18801`，替换为实际虚拟IP；内部令牌、接口路径和请求超时保持原值。网络需允许Java访问Python18801、Python访问原Redis端口。

## 安装生产依赖

在目标主机手工执行，进入提前准备好的`.venv`后安装依赖；不复制开发机虚拟环境，不安装测试依赖：

```bash
cd /data/content/be-data-analysis
source .venv/bin/activate
pip3.12 install --no-cache-dir -r requirements.txt
```

需要下载代理时，给这次pip命令添加`--proxy http://代理地址:端口`；systemd运行环境不参与依赖安装。

## 配置并启动服务

以下命令仅供部署人员在目标主机执行。从Docker切换时先停止旧容器，避免同时调度采集；已有原生服务更新前也先停止。

```bash
# 在原Compose项目目录执行；纯原生部署无需此步。
docker compose stop be-data-analysis

cd /data/content/be-data-analysis
# 以下权限对应默认root服务；其他账号使用对应主组及日志所有者。
sudo chown root:root .env.prod
sudo chmod 0640 .env.prod
sudo install -d -o root -g root -m 0750 /data/logs/be-data-analysis
sudo touch /data/logs/be-data-analysis/service.log
sudo find /data/logs/be-data-analysis -maxdepth 1 -type f -name 'service.log*' \
  -exec chown root:root {} + -exec chmod 0640 {} +
sudo systemd-analyze verify deploy/systemd/be-data-analysis.service
sudo logrotate --debug deploy/systemd/logrotate.conf
sudo install -m 0644 deploy/systemd/be-data-analysis.service /etc/systemd/system/be-data-analysis.service
sudo install -m 0644 deploy/systemd/logrotate.conf /etc/logrotate.d/be-data-analysis
sudo systemctl daemon-reload
sudo systemctl enable --now be-data-analysis.service
sudo systemctl status be-data-analysis.service --no-pager
sudo tail -n 50 /data/logs/be-data-analysis/service.log
```

从Java主机验证`http://<PYTHON_VIRTUAL_IP>:18801/health`；健康接口会访问Redis，不采集真实源。修改`.env.prod`后重启服务，使新进程重新读取配置。更新依赖前先停止服务，备份后在已有环境中安装，再启动。服务异常5秒后重启，60秒内最多5次启动；排除错误后使用`systemctl reset-failed be-data-analysis`。

## 连接失败排查

健康路径为`/health`。`/heath`拼写错误会在成功连接后返回404；`curl: (7) Could not connect`发生在HTTP请求之前，不能据此判断Redis或业务接口故障。ping正常只说明ICMP可达，需查看TCP监听与实际服务状态。

先在**Python目标主机**执行以下只读检查，不在Java主机查看Python进程：

```bash
sudo systemctl status be-data-analysis.service --no-pager -l
sudo ss -lntp '( sport = :18801 or sport = :8000 )'
ip -br address
sudo journalctl -u be-data-analysis.service -n 30 --no-pager
sudo tail -n 30 /data/logs/be-data-analysis/service.log
# 只输出绑定地址和端口，不输出Redis密码或内部令牌。
sudo awk -F= '$1 ~ /^[[:space:]]*SERVICE_(HOST|PORT)[[:space:]]*$/ {print}' /data/content/be-data-analysis/.env.prod
```

unit将stdout/stderr写入文件，因此应用导入、配置加载、Redis启动或绑定失败等日志应同时查看`service.log`；journal主要用于systemd层面的启动故障。不要回传完整`.env.prod`或所有环境变量。

| 实际结果 | 对应处理 |
| --- | --- |
| 监听8000，没有18801 | 核对上述生产文件中的`SERVICE_PORT=18801`；若仍监听8000，检查已安装unit或drop-in是否有旧端口覆盖。修正后重启。 |
| 18801仅监听127.0.0.1 | 将`SERVICE_HOST`改为Python主机实际虚拟IP，修正后重启。 |
| 日志显示绑定地址不可用 | 对照`ip -br address`确认虚拟IP确实配置在Python主机；修正主机IP配置或绑定地址后重启。 |
| 没有监听，unit启动失败 | 按journal和文件日志处理：确认WorkingDirectory及虚拟环境解释器路径存在、生产依赖齐全、源码及配置可读、日志目录可写。不要直接反复重试。 |
| Python主机通过虚拟IP可连接，Java主机仍失败 | 检查两机之间18801/TCP的路由、访问策略及防火墙；仅核查所需端口。 |

对已经查明并修正的配置或启动问题，使用以下恢复命令。仅修改`.env.prod`无需daemon-reload；修改已安装的unit或drop-in才需要先执行`sudo systemctl daemon-reload`。

```bash
sudo systemctl reset-failed be-data-analysis.service
sudo systemctl restart be-data-analysis.service
sudo ss -lntp '( sport = :18801 )'
# 将<PYTHON_VIRTUAL_IP>替换为实际Python虚拟IP，在Python和Java主机分别验证。
curl --noproxy '*' --connect-timeout 5 --max-time 10 -i 'http://<PYTHON_VIRTUAL_IP>:18801/health'
```

curl返回HTTP响应说明TCP及HTTP链路已建立；`/health`返回503时再检查Redis健康问题。上述命令未在目标服务器执行，根因须以目标主机输出为依据。

## 日志和停止

单进程运行`python -m app serve`，不reload、不使用nohup。stdout/stderr追加写入`/data/logs/be-data-analysis/service.log`，不新增Python文件handler；服务和源子进程共用输出。原生轮转每天执行，保留14份，压缩并延迟压缩，使用copytruncate；复制及截断期间存在短暂日志丢失窗口。Docker与原生轮转配置二选一，不重复处理同一文件。

使用`systemctl stop be-data-analysis`正常停止：Uvicorn最多等待30秒，生命周期取消调度及采集，源进程TERM后必要时KILL并回收。unit使用`KillMode=mixed`，最多60秒后清理剩余进程；不根据SIGKILL推断OOM。人工stop不会触发异常重启。采集业务、auto/manual、共享限速/冷却及Redis契约保持原逻辑。

## 回退Docker

先停止原生服务，恢复原Docker日志账号与轮转配置，再启动保留容器；不清空Redis或手动清除采集锁/冷却键。

```bash
sudo systemctl disable --now be-data-analysis.service
sudo chown 10001:10001 /data/logs/be-data-analysis
sudo find /data/logs/be-data-analysis -maxdepth 1 -type f -name 'service.log*' \
  -exec chown 10001:10001 {} +
# 在原Compose项目目录执行。
sudo install -m 0644 deploy/logrotate.conf /etc/logrotate.d/be-data-analysis
docker compose up -d --no-build be-data-analysis
```

容器继续使用内部8000/宿主机18801映射和`SERVICE_HOST=0.0.0.0`。本次仅整理原生部署文件，未在目标Linux主机实际安装、启停、轮转或验证网络；静态检查不等同部署成功。
