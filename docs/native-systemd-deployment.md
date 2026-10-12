# Linux systemd 原生部署

这是一种与现有Docker部署并列的方式。默认应用目录`/data/content/be-data-analysis`，默认账号/主组`be-data-analysis`，也可通过`RUN_USER`选择root或其他账号，仅一个`python -m app serve`服务进程，不reload、不使用nohup。行情、资金和日历调度沿现行逻辑，源调用仍独立spawn；auto/manual、共享HTTP限速/冷却、REST/SSE与Redis V1键均不变。

本次移除Python内存采样、阈值准入及按内存阈值取消源请求；不设置systemd内存限制或替代监控。仍保留源异常退出RESOURCE分类、退出码/信号、超时及TERM/KILL/reap、Redis失败停止和风控。SIGKILL不能直接推断为OOM。

## 主机与网络准备

- 不限制Ubuntu版本或Linux发行版；主机需提供systemd、logrotate及脚本所用账号管理工具。提前将`python3.12`加入PATH，并提供其venv/pip组件；不替换系统解释器。安装脚本只使用已有解释器，不自动新增软件源或安装Python。
- Java的admin、scheduler、openapi三处生产配置改为访问`http://<PYTHON_VIRTUAL_IP>:18801`，内部令牌与原请求超时不变；将占位符替换为实际分配的Python虚拟IP。Python的`SERVICE_HOST`绑定这个IP，主机必须已配置该IP且允许Java访问18801。
- Python仍连接原Redis虚拟IP及DB2，按既有网络策略允许Python到该Redis端口。保留真实密码、雪球Token和内部Token；不改变Redis数据，不flush、不手动清锁。
- 上传本仓库`app/`、`requirements.txt`及`deploy/systemd/`到默认目录；**不上传本机`.venv`**。目标主机重新创建虚拟环境，只安装生产依赖`requirements.txt`，不安装`requirements-dev.txt`或测试模块。
- 应用目录与源码可由root/部署管理员管理，但选定运行账号必须能遍历父目录、读取源码。安装脚本会检查入口可读；它只使`.venv`和日志目录归运行账号所有，不递归修改源码所有权。

## 生产配置

在目标应用目录保留已有私有`.env.prod`。可参照`deploy/systemd/env.prod.example`，但**不要复制样例覆盖已有生产文件**。只在主机上修改部署字段：

```dotenv
SERVICE_HOST=填写Python虚拟IP
SERVICE_PORT=18801
SERVICE_LOG_LEVEL=info
REDIS_URL=填写原Redis虚拟IP
REDIS_PORT=6379
REDIS_DB=2
```

Redis密码和其他私有字段沿用原值，不输出配置内容到工单或终端回传。原生主机连接Redis虚拟IP，不能继续使用仅在Docker中映射的`host.docker.internal`。

systemd只注入`APP_ENV=prod`、固定端口18801、时区、无缓冲输出和三个数值库线程数1。`.env.prod`由应用现有dotenv加载，不由shell source或systemd EnvironmentFile二次解析；密码不做变量展开。`SERVICE_HOST`由该文件提供。systemd的`SERVICE_PORT=18801`优先于文件值；文件同时设18801方便手工检查。现有Compose仍覆盖容器端口8000，其文件保持不变。本地默认`.env.dev`不变。

## 安装与启用（仅在目标主机人工执行）

源码和私有配置就位后，在**已停止的目标部署**上执行。从Docker切换时，先执行下一节的`docker compose stop be-data-analysis`，再运行安装脚本，以免运行期间改变日志账号。安装脚本拒绝覆盖已有`.venv`；如原目录中有虚拟环境，先停止服务并备份该目标主机目录，再重新创建，不复制本机环境。

```bash
cd /data/content/be-data-analysis
# 如目标主机已有.venv，先备份：
# sudo mv .venv .venv.backup-YYYYMMDD-HHMMSS
# 默认使用PATH中的python3.12，以be-data-analysis账号运行服务。
sudo env PATH="$PATH" bash deploy/systemd/install.sh
# 如选择root运行服务，将上面的安装命令替换为：
# sudo env PATH="$PATH" RUN_USER=root bash deploy/systemd/install.sh
# 如选择已有账号（例如appuser），将上面的安装命令替换为：
# sudo env PATH="$PATH" RUN_USER=appuser bash deploy/systemd/install.sh
```

脚本复用选定账号及其主组；账号不存在时创建系统账号/同名主组。创建目标`.venv`后，以该账号执行虚拟环境中的`pip3.12`安装生产依赖，不使用全局pip安装。服务unit、日志轮转及文件权限均使用同一账号/主组；生产配置仅调整为root:运行主组、0640，不写入配置值。日志目录为该账号所有、0750，已有`service.log`及同名前缀归档只调整权限/所有权，不截断内容。脚本会静态验证unit、debug检查logrotate、安装unit与轮转配置、daemon-reload并enable开机启动，**不启动服务、不操作Docker**。

仅新创建的系统账号以应用目录为home并禁止登录；已有账号（包括root）保持原账号设置。脚本不删除旧环境；依赖安装失败时先处理具体错误，不在残缺环境上直接启动。自定义Python安装位置时将`PYTHON_BIN`设为PATH中的命令名或该主机绝对路径，版本必须为3.12。

## 切换与检查

按顺序切换，避免旧容器与原生服务同时进行自动采集。以下命令由部署人员执行，本轮没有执行：

```bash
# 先在旧Docker项目目录停止采集容器，保留镜像和文件以便回退。
docker compose stop be-data-analysis
# 迁移日志所有权后不得再启动旧容器，直到完成回退权限恢复。
sudo systemctl start be-data-analysis.service
sudo systemctl status be-data-analysis.service --no-pager
sudo systemctl is-enabled be-data-analysis.service
sudo systemctl show be-data-analysis.service -p MainPID -p SubState -p ExecMainStatus
sudo tail -n 50 /data/logs/be-data-analysis/service.log
```

从Java主机验证`http://<PYTHON_VIRTUAL_IP>:18801/health`。健康检查会访问Redis而不采集真实源；应在授权的部署验收中执行。本轮未访问真实Redis。核对日志时间/PID/task，确认只有一个服务实例。Java沿用内部Token、原路径和同步请求预算；八个本机scheduler回环刷新携带manual，admin/定时仍auto，公共GET/SSE不改。

更新代码/依赖前先`systemctl stop`；保留备份，再按前述步骤创建新的目标虚拟环境。修改`.env.prod`后`systemctl restart be-data-analysis`，新进程重新读取文件。失败启动短时间最多5次尝试，排除错误后可`systemctl reset-failed be-data-analysis`再启动，不反复重试源接口。

## 停止与子进程

正常停止使用`sudo systemctl stop be-data-analysis`。Uvicorn停止接收新请求并最多等待30秒处理已有请求；生命周期取消自动调度，采集入口cancel传到执行器，源TERM后必要时KILL并join/close。unit使用`KillMode=mixed`：先向服务主进程发送SIGTERM，最多60秒后或主进程退出时清除剩余子进程，避免孤儿采集进程。不得将强制停止后的SIGKILL日志解释为OOM。该设置符合[systemd.kill说明](https://manpages.ubuntu.com/manpages/jammy/de/man5/systemd.kill.5.html)。

systemd异常重启间隔5秒；人工stop不会按on-failure重启。没有ExecReload、多worker或外部采集cron。长时间手动请求在服务停止时可能被取消，原同步响应不保证交付；已完成的事务数据保留，未发布数据不补写。

## 日志与轮转

stdout以append写入`/data/logs/be-data-analysis/service.log`，stderr继承同一输出；源子进程继承输出。使用既有上海时间StreamHandler及Uvicorn日志格式，**没有新增Python文件handler**。append不截断旧文件，见[Ubuntu 22.04 systemd.exec说明](https://manpages.ubuntu.com/manpages/jammy/man5/systemd.exec.5.html)。

原生轮转文件`deploy/systemd/logrotate.conf`与服务使用相同账号/主组，daily、14份、压缩并延迟压缩、copytruncate，无需服务重启。安装到`/etc/logrotate.d/be-data-analysis`，与Docker轮转配置二选一，不能让两份规则重复处理同一日志。copytruncate复制/截断期间存在很短的日志丢失窗口，见[Ubuntu logrotate说明](https://manpages.ubuntu.com/manpages/jammy/man8/logrotate.8.html)；安装动作本身不轮转、不截断日志。

目标主机仅静态预检查可使用：

```bash
sudo systemd-analyze verify /etc/systemd/system/be-data-analysis.service
sudo logrotate --debug /etc/logrotate.d/be-data-analysis
```

## 回退Docker

先停止并禁用原生服务，确认原服务已停止，再恢复Docker日志账号权限及原轮转配置；然后在旧Compose目录启动保留的容器。不要flush Redis或删除尚未过期的采集锁/冷却键，遵守自然TTL；两种部署不得同时运行。

```bash
sudo systemctl disable --now be-data-analysis.service
# 原Compose使用UID/GID10001；仅改变日志元数据，不截断内容。
sudo chown 10001:10001 /data/logs/be-data-analysis
sudo find /data/logs/be-data-analysis -maxdepth 1 -type f -name 'service.log*' \
  -exec chown 10001:10001 {} +
# 在旧Compose项目目录恢复原Docker轮转规则并启动旧容器。
sudo install -m 0644 deploy/logrotate.conf /etc/logrotate.d/be-data-analysis
docker compose up -d --no-build be-data-analysis
```

原生安装不改Dockerfile、compose.yaml或deploy.sh，不构建镜像。旧容器启动前确认其配置仍能连接原Redis虚拟IP，容器内部8000/宿主机18801的既有映射不变。不要为了回退将原生SERVICE_HOST虚拟IP直接写入容器绑定地址；Compose现有SERVICE_HOST=0.0.0.0覆盖不变。

## 验证边界

本轮执行离线模拟测试、Python语法检查、Bash语法与unit/轮转静态约束检查，不调用真实源。开发主机无目标Linux systemd/logrotate环境，未在Ubuntu22.04实际安装、启停、重启、开机启动、轮转或验证虚拟IP/Redis网络。上线验收由部署人员在目标环境完成；不把静态检查等同实际部署成功。
