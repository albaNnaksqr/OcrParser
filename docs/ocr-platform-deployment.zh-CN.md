# OCR Platform 生产部署指南

本文档面向真实生产部署，默认控制端、执行端、共享盘和模型服务是分离的。
文中的主机名、路径、账号和模型 URL 都使用生产占位符；不要把测试机器名
或个人环境路径作为生产默认值。

## 优先使用 installer

生产主机建议先使用本机 installer：

```bash
sudo python3 tools/install_production.py control --dry-run
sudo python3 tools/install_production.py worker --dry-run
```

在中控/UI 主机运行 `control`，在每台执行机运行 `worker`。installer 会要求输入
已有 service user/group，校验目录和共享盘访问，默认使用本机 primary IP 作为
worker id，打印 install plan，并在确认后才写文件或重启服务。API 鉴权支持开启，
但第一次安装默认关闭。

## 启动入口

建议把平台启动方式明确分成三套，避免临时命令和生产服务混用：

- `本地开发`：直接运行 `python -m ocr_platform.control`。它可以使用 SQLite，
  只适合快速开发 UI/API 或临时看页面。
- `单机生产近似`：运行本地编排脚本。脚本启动容器 PostgreSQL，应用 SQL
  migration，打开带生产保护项的本机 control UI，并可选启动一个本机 worker。
- `真实生产`：使用生产 PostgreSQL、systemd 服务、多台执行机、共享盘和生产
  模型服务。

| 模式 | DB | 端口 | Env | 日志 | 停止 |
| --- | --- | --- | --- | --- | --- |
| `本地开发` | 默认 `sqlite:///./ocr_platform.db`，除非设置 `OCR_PLATFORM_DATABASE_URL` | control 默认 `8080` 或 `OCR_PLATFORM_PORT` | shell env，例如 `OCR_PLATFORM_HOST`、`OCR_PLATFORM_PORT`、`OCR_PLATFORM_DATABASE_URL` | 前台 stdout/stderr | 前台 shell 中 `Ctrl-C` |
| `单机生产近似` | `postgresql+psycopg://...@127.0.0.1:15432/ocr_platform` | control `38080`，PostgreSQL `15432` | `.local/production/control.env`，可选 `.local/production/worker.env` | `.local/production/logs/control.out.log`、`.local/production/logs/control.err.log`，可选 worker logs | `python3 tools/local_prod_env.py down` |
| `真实生产` | 生产 `OCR_PLATFORM_DATABASE_URL`，仅 PostgreSQL | control 通常 `8080`，PostgreSQL 内网 `5432`，worker 出站访问 control/model 服务 | `/etc/ocr-platform/control.env`、`/etc/ocr-agent/worker.env` | `journalctl -u ocr-platform-control`、`journalctl -u ocr-agent-worker`、`/var/log/ocr-agent` | `systemctl stop ocr-platform-control` 和 `systemctl stop ocr-agent-worker` |

单机生产近似命令：

```bash
python3 tools/local_prod_env.py up --with-worker --shared-root /tmp/ocr-shared
python3 tools/local_prod_env.py status
python3 tools/local_prod_env.py down
```

如果只是要做 UI 走查，而本机没有真实 OCR 模型服务，可以临时加上 mock
OpenAI-compatible endpoint：

```bash
python3 tools/local_prod_env.py up --with-worker --with-mock-ocr --shared-root /tmp/ocr-shared
```

mock 默认监听 `http://127.0.0.1:18000/v1`，模型名是 `mock-ocr`。它只用于验证
control、worker、distributed shard、event 和 Jobs UI 状态，不代表真实 OCR
质量或吞吐。提交走查任务时，在 UI 的 model profile 里使用
`engine=dotsocr`、`ip=127.0.0.1`、`port=18000`、`model_name=mock-ocr`。

默认 UI 是 `http://127.0.0.1:38080/ui/`，默认 API token 是
`local-dev-token`，本机 PostgreSQL 数据保存在
`.local/production/postgres-data`。可以先运行
`tools/local_prod_env.py up --dry-run` 检查计划。启动后运行
`tools/local_prod_env.py status` 查看实际 DB、端口、env、日志、健康探针和
停止命令。

不要让多个 control 进程同时占用同一个端口或数据库。如果之前用临时
`launchctl` 或前台命令启动过 control，应先停止旧进程，再使用单机生产近似
脚本。

## 生产拓扑

```text
控制端机器
  - 运行 OCR control API 和 UI
  - 连接生产数据库
  - 创建 job、folder snapshot、manifest、shard 和 recovery 状态

执行端机器池
  - 每台机器运行一个或多个 ocr_platform.agent worker
  - 定期 heartbeat 到控制端
  - 上报代码版本、Python 路径、共享盘可访问性和当前负载
  - 领取 shard 并调用模型服务处理 PDF

共享文件系统
  - 所有执行端机器必须看到同一套路径
  - 存放输入 PDF、manifest 和输出目录

模型服务
  - 独立于控制端和执行端运行
  - 通过生产负载均衡 URL 暴露给执行端
```

对于 `distributed folder scan`，控制端不需要直接访问 PDF 输入目录。真正需
要访问 `input_dir`、`output_dir` 和 `manifest_root` 的是执行端机器。

## 运行账号和共享盘权限

生产环境建议继续使用独立服务账号运行 control 和 agent，但共享盘权限不能
依赖“当前登录部署用户”是谁。应使用一个统一运行组来承接 OCR 平台在共享盘
上的读写权限：

```bash
# 所有挂载共享盘的机器必须使用一致的数字 UID/GID，或通过 LDAP/SSSD 统一管理。
sudo groupadd --system --gid 2400 ocr-runtime
sudo groupadd --system --gid 2401 ocr-platform
sudo groupadd --system --gid 2402 ocr-agent
sudo useradd --system --uid 2401 --gid ocr-platform --groups ocr-runtime \
  --create-home --home-dir /var/lib/ocr-platform --shell /bin/bash ocr-platform
sudo useradd --system --uid 2402 --gid ocr-agent --groups ocr-runtime \
  --create-home --home-dir /var/lib/ocr-agent --shell /bin/bash ocr-agent

# 可选：允许人工部署账号检查和创建生产批次目录。
sudo usermod -aG ocr-runtime "$USER"
```

上面的 UID/GID 只是示例；实际部署时选择环境中未占用的编号，并保证所有挂
载共享盘的机器一致。如果已有集中身份系统，就使用集中身份系统创建这些用
户和组，但仍保留 `ocr-runtime` 这个统一运行组模型。

`/shared/ocr-data` 是真实共享盘挂载点的占位符，例如你们环境中可能是
`/shared/ocr-data`。`input_dir` 和 `output_dir` 应按任务需要放在共享盘下的任
意业务目录；OCR 平台专属子目录只用于 manifest 和 job 级共享运行文件。给
这个平台子目录设置组可写和 setgid，确保后续新目录继承 `ocr-runtime` 组：

```bash
sudo mkdir -p /shared/ocr-data/ocr-platform/{manifests,jobs}
sudo chown -R root:ocr-runtime /shared/ocr-data/ocr-platform
sudo chmod 2775 /shared/ocr-data/ocr-platform
sudo find /shared/ocr-data/ocr-platform -type d -exec chmod 2775 {} +
sudo find /shared/ocr-data/ocr-platform -type f -exec chmod 0664 {} +
```

在控制端和每台执行端启动服务前，都要用服务账号身份验证共享盘，而不是只
用登录用户验证：

```bash
findmnt /shared/ocr-data
sudo -u ocr-agent test -r /shared/ocr-data/ocr-platform
sudo -u ocr-agent test -w /shared/ocr-data/ocr-platform
sudo -u ocr-platform test -r /shared/ocr-data/ocr-platform
sudo -u ocr-platform test -w /shared/ocr-data/ocr-platform
```

worker 配置使用真实共享盘根；job 的输入/输出使用业务目录，manifest 使用
平台专属目录：

```text
OCR_AGENT_SHARED_ROOTS=/shared/ocr-data
input_dir=/shared/ocr-data/project-a/pdfs
output_dir=/shared/ocr-data/project-a/output
manifest_root=/shared/ocr-data/ocr-platform/manifests
```

如果某台机器上的 `/shared/ocr-data` 显示为本机根分区而不是共享文件系统，应
先修复挂载，再启动 agent。

在修改生产服务前，可以从运维工作站或控制端运行只读巡检工具。该工具只通
过 SSH 执行读取类检查，不会写远端文件、重启服务或修复挂载：

```bash
python tools/production_preflight.py \
  --host control.example.internal \
  --host worker-1.example.internal \
  --host worker-2.example.internal \
  --host worker-3.example.internal \
  --user ocr_user \
  --identity-file ~/.ssh/ocr_prod_ed25519 \
  --shared-root /shared/ocr-data \
  --platform-root /shared/ocr-data/ocr-platform \
  --control-host control.example.internal \
  --control-url http://control.example.internal:8080 \
  --json
```

根据 JSON 输出确认 PostgreSQL/control API、共享盘挂载一致性、服务账号对
平台 manifest 目录的读写权限、agent 进程和部署 git 版本，再开始生产灰度
任务。

## 控制端生产部署

准备固定部署目录。如果已在上面的公共运行账号步骤中创建了
`ocr-platform`，这里不要重复创建该用户：

```bash
sudo mkdir -p /opt/ocr-platform /etc/ocr-platform /var/log/ocr-platform
sudo chown -R ocr-platform:ocr-platform /opt/ocr-platform /var/log/ocr-platform
```

部署代码和依赖：

```bash
sudo -iu ocr-platform
cd /opt/ocr-platform
git clone https://github.com/YOUR_ORG/ocrparser ocrparser
cd ocrparser
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

生产环境建议使用 PostgreSQL。SQLite 只适合本机开发或临时冒烟验证，不应作
为千万级 PDF 任务的生产数据库。

首次部署 PostgreSQL 时，先由 DBA 或运维创建数据库和账号，然后在控制端代码
目录按文件名顺序应用所有 SQL migration：

```bash
cd /opt/ocr-platform/ocrparser
python tools/apply_control_migrations.py \
  --database-url "$OCR_PLATFORM_DATABASE_URL"
```

基线 migration 会创建控制端核心表、`schema_migrations` 记录和生产查询索引；
增量 migration 会追加生产约束，例如 job 全局 shard index。后续 schema 变更
应继续追加新的 SQL migration，生产环境不要只依赖应用启动时的兼容升级逻辑。

服务启动后，可以通过控制端 API 验证当前数据库 migration 状态：

```bash
curl -H "Authorization: Bearer $OCR_PLATFORM_API_TOKEN" \
  http://ocr-control.internal:8080/api/system/database
```

响应会返回数据库 dialect、`schema_migrations` 表是否存在、代码仓库内已知
SQL migration、数据库实际已应用的 migration，以及 `latest_applied_migration`。
生产环境中 `dialect` 应为 `postgresql`，`latest_applied_migration` 应与当前
部署代码携带的最新 SQL migration 一致。

备份与恢复策略见 [OCR Platform 生产备份与恢复 Runbook](ocr-platform-backup-restore.zh-CN.md)。
生产备份必须同时覆盖 PostgreSQL 和共享盘 `manifest_root` 下的 JSONL manifest /
shard 文件；只备份其中一边都不足以恢复已执行的大任务。

在正式灰度前，建议对生产 PostgreSQL 或等价测试库执行 shard claim 并发验证：

```bash
cd /opt/ocr-platform/ocrparser
. .venv/bin/activate
python tools/pg_claim_stress.py \
  --database-url "$OCR_PLATFORM_DATABASE_URL" \
  --shards 1000 \
  --scan-units 1000 \
  --scan-unit-shards 2 \
  --workers 64 \
  --json
```

结果中的 `ok` 应为 `true`，`duplicate_claims` 应为空，`missing_claims` 应为
`0`，`attempt_conflict_rejected` 应为 `true`，并且 `scan_unit_claims.ok` 也
应为 `true`。加上 `--scan-unit-shards` 后，结果还应包含
`scan_unit_completion_shards.ok=true`，用于证明并发完成分布式 scan unit 时
生成的全局 shard index 连续且无重复。该脚本只接受 PostgreSQL DSN，用于验证真实数据库上的
`FOR UPDATE SKIP LOCKED` shard claim、分布式 scan-unit claim、
scan-unit complete、attempt-aware update 和关键索引是否能支撑并发领取与完成。首次对空测试库运行时可加
`--apply-init-db`；生产库应先按 migration 初始化。

从模板创建 `/etc/ocr-platform/control.env`，并限制权限：

```bash
cd /opt/ocr-platform/ocrparser
sudo cp configs/ocr-platform-control.env.example /etc/ocr-platform/control.env
sudo chown root:ocr-platform /etc/ocr-platform/control.env
sudo chmod 0640 /etc/ocr-platform/control.env
sudo editor /etc/ocr-platform/control.env
```

生产配置示例：

```bash
OCR_PLATFORM_DATABASE_URL=postgresql+psycopg://ocr_platform:CHANGE_ME@postgres.internal:5432/ocr_platform
OCR_PLATFORM_REQUIRE_POSTGRES=1
OCR_PLATFORM_REQUIRE_CURRENT_MIGRATIONS=1
OCR_PLATFORM_HOST=0.0.0.0
OCR_PLATFORM_PORT=8080
OCR_PLATFORM_API_TOKEN=CHANGE_ME_LONG_RANDOM_TOKEN
OCR_PLATFORM_REQUIRE_API_TOKEN=1
OCR_PLATFORM_DISABLE_SAVED_MODEL_PROFILE_KEYS=1

OCR_JOB_STALE_AFTER_SECONDS=120
OCR_SERVER_STALE_AFTER_SECONDS=120
OCR_SHARD_LEASE_SECONDS=300
OCR_SCAN_UNIT_CLAIM_BATCH_SIZE=100

OCR_JOB_FILE_DETAIL_LIMIT=10000
OCR_JOB_EVENT_DETAIL_LIMIT=50000
OCR_JOB_LOG_DETAIL_LIMIT=10000
OCR_JOB_FAILED_FILE_SAMPLE_LIMIT=100
OCR_JOB_RECENT_ERROR_SAMPLE_LIMIT=100
```

参数含义：

- `OCR_PLATFORM_DATABASE_URL`：控制端数据库连接串。生产建议 PostgreSQL。
- `OCR_PLATFORM_REQUIRE_POSTGRES`：生产建议设为 `1`。开启后 control 会拒绝
  SQLite 或其他非 PostgreSQL DSN，避免真实任务误跑到本地开发库。
- `OCR_PLATFORM_REQUIRE_CURRENT_MIGRATIONS`：生产建议设为 `1`。开启后
  control 在 PostgreSQL 上启动时会要求 `schema_migrations` 存在，并且当前
  代码携带的所有 SQL migration 都已应用；否则拒绝启动，避免 worker 在过期
  schema 上开始领取任务。
- `OCR_PLATFORM_HOST`：API/UI 监听地址，生产通常为 `0.0.0.0`，由防火墙或
  内网网关限制访问来源。
- `OCR_PLATFORM_PORT`：控制端端口。
- `OCR_PLATFORM_API_TOKEN`：控制端 API 共享鉴权 token。生产建议必须设置；
  设置后所有 `/api/` 请求都需要 `Authorization: Bearer <token>`、
  `X-API-Key: <token>` 或 `X-OCR-Platform-Token: <token>`。
- `OCR_PLATFORM_REQUIRE_API_TOKEN`：生产建议设为 `1`。开启后 control 会在
  `OCR_PLATFORM_API_TOKEN` 为空时拒绝启动，避免 API 裸跑。
- `OCR_PLATFORM_DISABLE_SAVED_MODEL_PROFILE_KEYS`：生产建议设为 `1`。开启后
  model profile API 会拒绝新写入直存数据库的 `saved_api_key`，编辑已有
  profile 时也会要求用 `clear_api_key=true` 清掉旧的直存 key。生产应改用
  `api_key_env_var`，让模型 API key 存在控制端进程 secret 环境中。
- `OCR_JOB_STALE_AFTER_SECONDS`：running job 多久没有新事件后被视为需要
  recovery 检查。
- `OCR_SERVER_STALE_AFTER_SECONDS`：worker heartbeat 多久未更新后显示为
  offline/stale。
- `OCR_SHARD_LEASE_SECONDS`：worker 失联后 shard 保留多久才允许被其他
  worker 重新领取。生产建议从 `300` 秒起，根据单个 shard 的正常耗时再调
  整。
- `OCR_SCAN_UNIT_CLAIM_BATCH_SIZE`：分布式扫描领取 scan unit 时，每次在
  PostgreSQL 上用 `FOR UPDATE SKIP LOCKED` 锁定的候选目录批大小。控制端会
  先锁定这一小批候选，再做 worker 路径可达性检查，避免千万级目录队列下
  无界拉取候选，同时防止多个扫描 worker 领取同一目录。
- `OCR_JOB_FILE_DETAIL_LIMIT`：控制端为每个 job 保留最近多少条 per-file
  明细。生产不要无限保留成功文件明细；千万级任务可以设为 `0`，关闭
  `job_files` 明细写入。
- `OCR_JOB_EVENT_DETAIL_LIMIT`：控制端为每个 job 保留最近多少条原始事件。
  聚合进度以 job/shard counters 为主，避免 page/file events 无界增长；千万
  级任务可以设为 `0`，关闭 `job_events` 明细写入。关闭后 Jobs summary 仍
  使用聚合计数、shard 计数和 manifest 计数展示总体进度；失败文件会在
  `job_counters` 中保留有界样本，供 `recent-files?kind=failed` 排障使用。
- `OCR_JOB_LOG_DETAIL_LIMIT`：控制端为每个 job 保留最近多少条 agent
  stdout/stderr 转发日志。长跑任务建议保持有界；设为 `0` 可关闭 `job_logs`
  明细写入，仍保留 agent 本机日志文件和 logrotate。
- `OCR_JOB_FAILED_FILE_SAMPLE_LIMIT`：当 per-file 明细行被关闭或裁剪时，
  每个 job 在 counter 中最多保留多少个最近失败文件样本。
- `OCR_JOB_RECENT_ERROR_SAMPLE_LIMIT`：当原始 event 明细关闭或裁剪时，每个
  job 在 counter 中最多保留多少个 job/shard 级失败事件样本，供
  `recent-errors/page` 排障使用。
生产 baseline migration 会同时创建这些有界明细表的关键索引：
`job_events(job_id, created_at, id)`、用于高频事件 upsert 的
`job_files(job_id, file_path)`、用于裁剪的
`job_files(job_id, updated_at, id)` 和 `job_logs(job_id, created_at, id)`，
避免高频写入和保留裁剪变成慢查询。

生产部署时，控制端 UI/API 必须能被执行端和运维人员通过内网地址访问。`127.0.0.1`
或 `localhost` 只代表控制端机器自己，适合本机开发，不适合作为生产入口。
因此：

- `OCR_PLATFORM_HOST` 建议设为 `0.0.0.0`，或设为控制端机器的内网 IP。
- 浏览器访问地址应使用 `http://<control-internal-host>:8080/ui/`。
- 执行端的 `OCR_CONTROL_URL` 应使用同一个内网可达地址，例如
  `http://ocr-control.internal:8080` 或 `http://10.x.x.x:8080`。
- 不要把生产 `OCR_CONTROL_URL` 配成 `http://127.0.0.1:8080`，否则 agent 会
  尝试访问执行端本机的 8080，而不是控制端。
- 防火墙、安全组或反向代理应只允许可信内网网段访问该端口。
- 若设置了 `OCR_PLATFORM_API_TOKEN`，执行端 `OCR_CONTROL_API_TOKEN` 必须使用
  同一个值。token 不要提交到 git，也不要写入普通日志。

安装 systemd：

```bash
cd /opt/ocr-platform/ocrparser
sudo cp services/ocr-platform-control.service.example /etc/systemd/system/ocr-platform-control.service
sudo systemctl daemon-reload
sudo systemctl enable --now ocr-platform-control
sudo systemctl status ocr-platform-control
```

打开 UI：

```text
http://ocr-control.internal:8080/ui/
```

不要使用 `file://.../index.html` 打开 UI，因为 file 模式不会稳定连接控制
端 API。

## 执行端 Agent 生产部署

下面步骤在每台执行端机器上执行。生产默认是一台执行机运行一个 agent；只
有在 CPU、内存、网络和共享盘 IO 都有余量时，才考虑单机多 agent。

### 1. 准备系统账号和代码

```bash
sudo mkdir -p /opt/ocr-platform /etc/ocr-agent /var/lib/ocr-agent /var/log/ocr-agent
sudo chown -R ocr-agent:ocr-agent /opt/ocr-platform /var/lib/ocr-agent /var/log/ocr-agent
```

部署代码：

```bash
sudo -iu ocr-agent
cd /opt/ocr-platform
git clone https://github.com/YOUR_ORG/ocrparser ocrparser
cd ocrparser
git checkout v2026.05.28
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

生产建议部署到 release tag 或明确 commit，而不是漂浮的开发分支。

### 2. 配置 Agent

从模板创建 `/etc/ocr-agent/worker.env`，并限制权限：

```bash
cd /opt/ocr-platform/ocrparser
sudo cp configs/ocr-agent-worker.env.example /etc/ocr-agent/worker.env
sudo chown root:ocr-agent /etc/ocr-agent/worker.env
sudo chmod 0640 /etc/ocr-agent/worker.env
sudo editor /etc/ocr-agent/worker.env
```

生产配置示例：

```bash
OCR_AGENT_SERVER_ID=worker-1.example.internal
OCR_CONTROL_URL=http://ocr-control.internal:8080
OCR_CONTROL_API_TOKEN=CHANGE_ME_LONG_RANDOM_TOKEN

OCR_REPO_DIR=/opt/ocr-platform/ocrparser
OCR_AGENT_WORK_DIR=/var/lib/ocr-agent/worker-1.example.internal
OCR_AGENT_PYTHON=/opt/ocr-platform/ocrparser/.venv/bin/python

OCR_AGENT_SHARED_ROOTS=/shared/ocr-data

OCR_AGENT_POLL_INTERVAL=2
OCR_AGENT_HEARTBEAT_INTERVAL=5
OCR_AGENT_CONTROL_RETRY_INITIAL=1
OCR_AGENT_CONTROL_RETRY_MAX=30
OCR_AGENT_EVENT_SPOOL_DIR=/var/lib/ocr-agent/worker-1.example.internal/event-spool
OCR_AGENT_EVENT_SPOOL_MAX_MB=1024
OCR_AGENT_TERMINATION_TIMEOUT=10
OCR_AGENT_STOP_POLL_INTERVAL=1
OCR_MANIFEST_SCAN_PROGRESS_INTERVAL_FILES=10000

OCR_AGENT_RUNNER=tmux
OCR_AGENT_LOG_DIR=/var/log/ocr-agent/worker-1.example.internal
OCR_AGENT_TMUX_SESSION=ocr-agent-worker-1.example.internal

OCR_AGENT_GIT_REF=v2026.05.28
OCR_AGENT_SCRIPT_VERSION=ocr-agent-worker-v1
```

`OCR_AGENT_EVENT_SPOOL_MAX_MB` 限制 control 不可用时每个 pending 本地
event/log spool 文件的大小。默认值是每个 pending 文件 `256` MiB；有独立持久盘的
worker 可以调大，设置为 `0` 表示不限制。超限时 agent 保留最新记录、丢弃最旧记录，
并在 heartbeat capabilities 中上报 `dropped_events` / `dropped_logs` 以及 spool
字节数，让 control UI 和预检警告显式暴露这类数据丢弃。

关键规则：

- `OCR_AGENT_SERVER_ID` 必须全局唯一。installer 默认使用本机 primary IP，
  例如 `worker-1.example.internal`；需要覆盖时再显式传 `--server-id`。
- `OCR_CONTROL_URL` 必须能从执行端访问控制端。
- `OCR_REPO_DIR`、`OCR_AGENT_PYTHON` 必须指向执行端本机的真实部署路径。
- `OCR_AGENT_SHARED_ROOTS` 是冒号分隔的共享盘根路径，例如
  `/shared/ocr-data:/mnt/ocr-archive`。
- UI 中的 `input_dir`、`output_dir`、`manifest_root` 必须位于某个 shared
  root 下。
- `OCR_MANIFEST_SCAN_PROGRESS_INTERVAL_FILES` 控制 distributed folder scan
  扫描阶段每发现多少个 PDF 上报一次 `manifest_scan_progress` 事件。
  Job summary 会使用最新进度事件里的扫描计数，同时从最近若干进度事件保留
  有界扫描错误样本；后续没有 `skipped_errors` 的进度事件不会在
  `skipped_error_count` 仍非零时把之前采样到的错误从 UI 中抹掉。即使本次
  扫描发现 0 个 PDF，scanner 也会发送最终 `done` 进度事件，让权限或 stat
  错误进入控制端摘要，而不是只留在 manifest metadata 文件里。control-host
  和 distributed folder snapshot metadata 都会记录真实
  `skipped_error_count`，但 `skipped_errors` 只保留有界样本，避免权限异常
  子树把 `manifest.meta.json` 撑成无界文件。没有 live progress event 时，
  Job summary 会回退读取这份 metadata，因此 control-host `folder_snapshot`
  job 也能在 API/UI 中显示扫描完成和采样扫描错误。
- `OCR_AGENT_EVENT_SPOOL_DIR` 是 agent 本地事件缓冲目录。控制端短暂不可用
  或返回 5xx 时，job event 会先写入这里；后续 heartbeat 成功后自动重放。
  这个目录必须放在执行机本地持久盘，建议位于对应 worker 的
  `OCR_AGENT_WORK_DIR` 下，不要放到会随重启清空的临时目录。待重放事件位于
  `events.jsonl`；如果某条旧事件重放时遇到永久 4xx 错误，agent 会把它移到
  `events.failed.jsonl` 并继续重放后续事件，避免一条坏事件堵住新的进度上报。
  运维排障时需要检查 `events.failed.jsonl`。

### 3. 启动和托管 Agent

先做本机检查：

```bash
cd /opt/ocr-platform/ocrparser
scripts/ocr_agent_worker.sh doctor /etc/ocr-agent/worker.env
```

生产建议用 systemd 托管：

```bash
cd /opt/ocr-platform/ocrparser
sudo cp services/ocr-agent-worker.service.example /etc/systemd/system/ocr-agent-worker.service
sudo systemctl daemon-reload
sudo systemctl enable --now ocr-agent-worker
sudo systemctl status ocr-agent-worker
```

安装 worker 文件日志轮转：

```bash
sudo cp services/ocr-agent-worker.logrotate.example /etc/logrotate.d/ocr-agent-worker
sudo logrotate -d /etc/logrotate.d/ocr-agent-worker
```

常用运维命令：

```bash
cd /opt/ocr-platform/ocrparser
scripts/ocr_agent_worker.sh status /etc/ocr-agent/worker.env
scripts/ocr_agent_worker.sh logs /etc/ocr-agent/worker.env
scripts/ocr_agent_worker.sh restart /etc/ocr-agent/worker.env
```

如果 agent 已经完全退出，控制端不能通过 HTTP 把远端进程启动回来，因为远
端已经没有可接收命令的进程。生产自恢复应由 systemd、Kubernetes 或其他本
机 supervisor 负责。

### 4. 单机多 Worker 部署

平台支持在同一台执行机上启动多个 worker。生产默认仍建议“一台机器一个
worker”；当物理执行机数量较少，但 CPU、内存、网络、共享盘 IO 都有余量，
并且远端模型 API 没有被打满时，可以用单机多 worker 横向增加请求流。

单机多 worker 的核心规则：

- 每个 worker 必须有唯一的 `OCR_AGENT_SERVER_ID`。
- 每个 worker 必须有独立的 `OCR_AGENT_WORK_DIR`、`OCR_AGENT_LOG_DIR` 和
  `OCR_AGENT_TMUX_SESSION`。
- 多个 worker 可以共用同一份 `OCR_REPO_DIR` 和同一个 Python venv。
- 多个 worker 应上报相同的 `OCR_AGENT_SHARED_ROOTS`，否则同一个共享盘
  job 可能只有部分 worker 可领取 shard。
- UI 中每个 worker 仍然会显示为独立执行机；distributed job 可通过
  `selected workers` 精确选择本机上的一部分 worker。

示例：一台机器上启动 4 个 worker。

```bash
sudo mkdir -p /etc/ocr-agent

for i in 1 2 3 4; do
  sudo cp /etc/ocr-agent/worker.env /etc/ocr-agent/worker-${i}.env
  sudo sed -i "s/^OCR_AGENT_SERVER_ID=.*/OCR_AGENT_SERVER_ID=ocr-worker-a-${i}/" /etc/ocr-agent/worker-${i}.env
  sudo sed -i "s#^OCR_AGENT_WORK_DIR=.*#OCR_AGENT_WORK_DIR=/var/lib/ocr-agent/worker-${i}#" /etc/ocr-agent/worker-${i}.env
  sudo sed -i "s#^OCR_AGENT_LOG_DIR=.*#OCR_AGENT_LOG_DIR=/var/log/ocr-agent/worker-${i}#" /etc/ocr-agent/worker-${i}.env
  sudo sed -i "s/^OCR_AGENT_TMUX_SESSION=.*/OCR_AGENT_TMUX_SESSION=ocr-agent-worker-${i}/" /etc/ocr-agent/worker-${i}.env
  sudo chmod 0640 /etc/ocr-agent/worker-${i}.env
done
```

使用标准脚本启动验证：

```bash
cd /opt/ocr-platform/ocrparser

for i in 1 2 3 4; do
  scripts/ocr_agent_worker.sh doctor /etc/ocr-agent/worker-${i}.env
  scripts/ocr_agent_worker.sh start /etc/ocr-agent/worker-${i}.env
done
```

如果用 systemd 托管，建议创建实例化 service：

```bash
sudo cp services/ocr-agent-worker@.service.example /etc/systemd/system/ocr-agent-worker@.service
sudo systemctl daemon-reload

for i in 1 2 3 4; do
  sudo systemctl enable --now ocr-agent-worker@worker-${i}
done
```

检查：

```bash
for i in 1 2 3 4; do
  sudo systemctl status ocr-agent-worker@worker-${i}
done

curl http://ocr-control.internal:8080/api/servers | python3 -m json.tool
```

单机多 worker 时，不要把每个 worker 都按单 worker 的最大 CPU 参数配置。
例如一台 20 逻辑核机器运行 4 个 worker 时，可以先从下面的任务参数开始：

```text
worker_count_per_host: 4
page_concurrency_per_worker: 80-120
file_concurrency_per_worker: 4-8
num_cpu_workers_per_worker: 8-16
capacity_slots_per_worker: 1
```

这样总 API inflight 由多个 worker 叠加，而不是让单个 worker 暴力拉高到
很大的 `page_concurrency`。如果发现执行机 CPU、内存、磁盘 IO 或共享盘
metadata IOPS 上升明显，应优先减少 worker 数或下调每个 worker 的
`num_cpu_workers`、`file_concurrency`。

对于 96 逻辑核、约 376 GiB 内存的高配生产执行机，可以更大胆地做阶梯压
测。建议从 4 个 worker 起步，再根据吞吐和错误分类逐步上调：

```text
initial:
  worker_count_per_host: 4
  page_concurrency_per_worker: 120
  file_concurrency_per_worker: 8
  num_cpu_workers_per_worker: 20-24
  capacity_slots_per_worker: 1

aggressive:
  worker_count_per_host: 4
  page_concurrency_per_worker: 160
  file_concurrency_per_worker: 8
  num_cpu_workers_per_worker: 20-24
  capacity_slots_per_worker: 1

scale_out:
  worker_count_per_host: 6
  page_concurrency_per_worker: 120-160
  file_concurrency_per_worker: 8
  num_cpu_workers_per_worker: 14-16
  capacity_slots_per_worker: 1
```

这类机器的目标不是一开始把 CPU 打满，而是观察增加 worker 后总吞吐是否接
近线性增长，同时确认错误类型没有恶化。建议重点观察：

```text
cpu_load: 不长期超过 70-80%
memory_percent: 不超过 70%
api_error_categories.timeout/network/http_status/rate_limit: 接近 0
api_error_categories.model_output: 不随并发明显升高
oldest_api_inflight: 不持续超过 120-180s
total_page_per_second: 随 worker 数增加接近线性增长
```

## 共享盘生产要求

所有参与同一个分布式任务的执行端机器，必须看到完全一致的共享路径。例如
UI 中填写：

```text
input_dir=/shared/ocr-data/project-a/pdfs
output_dir=/shared/ocr-data/project-a/output
manifest_root=/shared/ocr-data/ocr-platform/manifests
```

那么所有被选中的 worker 都需要：

- 能读取 `/shared/ocr-data/project-a/pdfs`
- 能写入 `/shared/ocr-data/project-a/output`
- 能写入 `/shared/ocr-data/ocr-platform/manifests`

输入目录可以包含多层嵌套目录。distributed folder scan 会在执行端以
streaming 方式递归扫描 PDF，边扫描边写 manifest 和 shard 文件，不会把千
万级路径一次性放进控制端数据库。生产中如果输入目录会持续变化，建议把每
次生产批次对应到一个不可变目录或显式版本化 manifest，避免同一个 job 的
输入集合在运行中变化。

## 模型服务生产配置

模型服务应通过生产负载均衡地址暴露给执行端。控制端 UI 的 model profile
负责把模型配置写入 job；执行端不应在代码里写死模型地址。

生产配置请使用真实内网域名或负载均衡地址，例如：

```text
DotsOCR:
  engine=dotsocr
  endpoint=http://dotsocr-lb.internal:13080
  model_name=DotsOCR

MinerU:
  engine=mineru
  endpoint=http://mineru-lb.internal:30090

PaddleOCR-VL:
  engine=paddleocr-vl
  endpoint=http://paddleocr-vl-lb.internal:30001
```

API key 等敏感信息不要提交到 git。可以在提交任务时输入，或接入生产密钥管
理机制。

生产上更建议在 model profile 配置 `api_key_env_var`。控制端会在创建 job 和
执行端领取 `next-job` 时解析这个环境变量，因此真实模型 API key 可以放在进
程 secret 环境里，而不是写入控制端数据库。生产建议开启
`OCR_PLATFORM_DISABLE_SAVED_MODEL_PROFILE_KEYS=1`，阻止 profile 编辑继续写入
或保留直存 DB 的 key，同时拒绝新 job 直接保存 `extra_args.api_key`。如果开发环境直接在 model profile 中保存 API key，它
仍然是控制端数据库里的 profile secret；创建 job 时不会复制到 job 的
`extra_args_json`，普通 job API 响应也不会回显。执行端通过 `next-job` 领取
任务时，控制端会在响应中临时注入运行所需的 API key。对于不使用 model
profile 的一次性 job，也应传 `extra_args.api_key_env_var`，不要传
`extra_args.api_key`；开启生产 guard 后，`extra_args.api_key` 会被拒绝。控制端
会在创建 job 时校验 env var，只把 env var 名写入 DB，并只在 `next-job` 响应中
注入解析后的 key。agent 会通过子进程 `API_KEY` 环境变量把 key 交给 parser，
不会写进 `--api_key` argv 或本地 `command.json` 记录。因此，生产上仍需要限制控制端
数据库、控制端 API token 和执行端日志权限；不要把 agent 领取到的 job payload
原样写入普通业务日志。不要把 `api_key` 写进 model profile 的
`extra_args JSON`；应使用 `api_key_env_var` 或专门的 `saved_api_key` 字段，
后端也会拒绝这种配置。后端同样会拒绝 profile `extra_args` 中的 token、
password、authorization、client secret 等 secret-like key。job `extra_args`
只允许通过专用的 `api_key` / `api_key_env_var` 通道传递 secret；其他
secret-like key 会被控制端 API 拒绝，agent 构造子进程命令时也会忽略它们，
避免落入 argv 或 `command.json`。

## 生产任务参数建议

在控制端 UI 创建 distributed job 时：

1. `execution_mode` 选择 `distributed folder scan`。
2. 选择生产 model profile。
3. `worker_scope` 选择 `all eligible workers` 或 `selected workers`。
4. 填写共享盘上的 `input_dir`、`output_dir`、`manifest_root`。
5. 设置 `target_files_per_shard`、`page_concurrency`、`num_cpu_workers`。
6. 确认 path check 中 eligible/ready worker 符合预期后创建任务。

如果输入目录是千万级 PDF，并且目录下有足够多的子目录可以切分，可以选择
`distributed manifest scan`。这个模式会先在控制端创建 scan unit 队列：
worker 领取一个目录，只扫描该目录的直接 PDF，并把子目录提交回控制端作为
新的 scan units。这样多个执行端可以并行展开目录树。这个模式仍然受共享盘
metadata IOPS 限制；它能把扫描工作分摊到多台机器，但不能绕过共享盘本身
的元数据吞吐上限。

当前建议的生产基线：

```text
target_files_per_shard: 1000-5000
page_concurrency: 80
file_concurrency: 8
num_cpu_workers: 56
max_shard_attempts: 3
OCR_SHARD_LEASE_SECONDS: 300
OCR_SCAN_UNIT_CLAIM_BATCH_SIZE: 100
```

以上基线按“一台执行机一个 worker”理解。如果同一台机器启动多个 worker，
应把 `page_concurrency`、`file_concurrency`、`num_cpu_workers` 视为每个
worker 的参数，并按 worker 数下调，避免多个进程池在同一台机器上过量竞争。

这些值不是所有机器的硬性默认。若执行机 CPU、内存、共享盘 IO 或模型服务
负载不足，应按实际容量下调。正式千万级任务前，建议先用同一套生产配置跑
一次灰度批次，例如 100-1000 个 PDF，确认吞吐、失败率和 recovery 都符合预
期，再扩大到完整目录。

## 上线前验收

控制端检查：

```bash
curl http://ocr-control.internal:8080/api/servers | python3 -m json.tool
curl 'http://ocr-control.internal:8080/api/jobs/page?limit=50&offset=0' | python3 -m json.tool
curl http://ocr-control.internal:8080/api/jobs/summary | python3 -m json.tool
curl 'http://ocr-control.internal:8080/api/jobs/summary/page?limit=50&offset=0' | python3 -m json.tool
```

`/api/jobs` 和 `/api/jobs/summary` 保留兼容旧客户端的列表响应。生产脚本和 UI
可使用 `/api/jobs/page` 或 `/api/jobs/summary/page`；分页响应包含
`items`、`total`、`limit`、`offset` 和 `has_more`，这样 job 历史很多时也能
分页显示，不需要靠返回条数猜测是否还有下一页。

创建生产 job 前，建议先在 UI 点 `Preflight`，或直接调用：

```bash
curl -X POST http://ocr-control.internal:8080/api/jobs/preflight \
  -H 'Content-Type: application/json' \
  -d '{
    "model_profile_id": "dotsocr_15",
    "input_dir": "/shared/ocr-data/project-a/pdfs",
    "output_dir": "/shared/ocr-data/project-a/output",
    "engine": "dotsocr",
    "input_mode": "distributed_remote_folder_snapshot",
    "manifest_root": "/shared/ocr-data/.ocr_platform/manifests"
  }' | python3 -m json.tool
```

`ok=false` 表示存在阻断项，例如没有可用 worker、model profile 缺 API key、
`output_dir` 不可写、`manifest_root` 不可写，或 PostgreSQL 控制库的 SQL
迁移尚未应用；`warning` 项会提示 SQLite、worker 版本不一致、eligible
worker 当前资源受压或过高的明细保留配置等生产风险。Preflight 会基于已上报的 worker shared path 与资源 heartbeat 判断路
径可读/可写，以及被选 worker 是否已经处于 CPU、内存或磁盘压力之下。
直接创建 job 的 API 也会拒绝迁移未就绪的 PostgreSQL 任务，脚本提交不能绕过
这条数据库保护。对于 selected 或 eligible 的分布式 worker，所有能读取 `input_dir` 的 worker 都必须能写 `output_dir` 和
`manifest_root`，否则会在创建 job 前阻断。如果未填写 `manifest_root`，
preflight 会检查按共享盘推断出的默认路径，例如
`/shared/ocr-data/.ocr_platform/manifests`。执行端 heartbeat 信息必须是最新的。

资源 guard 不只影响领取新 shard。agent 会在
`OCR_AGENT_WORK_DIR/jobs/<job-id>/execution-control.json` 写入运行时控制文件，
并把该文件通过 `--execution_control_file` 传给 parser。资源受压时，正在运行
的 shard 会暂停启动新的模型 API 调用，并把 API 并发上限临时降到 `1`；已在
途的调用会正常结束。压力解除后，agent 会恢复 `paused=false`，并把并发上限
恢复到该 job 的 `api_concurrency_start`、`api_concurrency_max` 或
`page_concurrency`。agent 也会把每次 execution-control 变化同步到当前
shard 行，同时保留已有进度计数，因此 parser 发出下一条 file event 前，UI
也能看到 pause/restore 状态。这属于协作式暂停，不是强杀；需要终止 shard 时仍使用 stop
请求。Job summary UI 和 shard inspector 会展示 execution paused/running、
当前 API 并发上限和压力原因，便于区分正常 drain 与真正卡住的 shard。
Shard inspector 使用服务端分页，并支持按 status、worker、`failure_category`、
最小 attempt 次数和 running 时长过滤；事故排查时可以聚焦 failed、stale、
retrying shard，而不需要把全部 shard 一次性加载到浏览器。生产 migration
基线会创建 `work_shards(job_id, failure_category, status, shard_index)` 和
`work_shards(job_id, status, started_at, shard_index)` 索引，让这些事故筛选在
大任务上仍然有界。

执行端检查：

```bash
cd /opt/ocr-platform/ocrparser
scripts/ocr_agent_worker.sh doctor /etc/ocr-agent/worker.env
scripts/ocr_agent_worker.sh status /etc/ocr-agent/worker.env
```

需要确认：

- 所有预期 worker 都在线，没有 stale。
- `git_ref`、`script_version`、`python_path` 符合本次发布。
- shared path 检查为绿色。
- 分布式 job 的 shards 能被多个 worker 领取。
- output 目录能看到产出文件。
- 控制端重启后，agent 会重连；agent 异常退出后，systemd 会拉起。

产物完整性抽检或全量审计：

```bash
cd /opt/ocr-platform/ocrparser
python3 tools/audit_manifest_outputs.py \
  --manifest /shared/ocr-data/.ocr_platform/manifests/<job-id>/shards/shard-000001.jsonl \
  --output-dir /shared/ocr-data/output/<job-id> \
  --check-input
```

该工具直接读取 manifest/shard JSONL，按 `output_dir + relative_path` 检查每个
PDF 对应的 `.ocr_status.json` 和声明的 markdown/json/pdf 等产物，不依赖
`job_files` 保存全量文件状态。返回 JSON 报告；发现缺 sidecar、缺 artifact、
半成品、输入文件变更、重复 `relative_path` 等问题时退出码为 `1`。千万级任
务可以先对少量 shard 抽检，必要时去掉抽样限制做批次级全量审计。若使用
`--max-items`，报告中的 `truncated` 会为 `true`，表示这只是前 N 行抽检结果，
不能当作全量通过。`--sample-limit` 只限制 JSON 中保留的错误样本数量；若
`issue_samples_truncated=true`，说明还有更多同类或其他问题未列入样本，应以
`issues_by_category` 和 `issue_count` 作为聚合判断。对于失败 sidecar，issue
sample 会包含 sidecar 的 `failure_category` 和 `error_type`，排查 retry 时可
先看 audit JSON，不必逐个打开输出目录。
manifest/shard 重跑时，只有成功 sidecar 的输入快照与 manifest 行一致，且其
声明产物仍位于该 PDF 的输出目录内、仍存在、非空，并且 JSON/JSONL 产物可正常解析，才会复用旧输出；如果 sidecar 缺少
`input_size_bytes` / `input_mtime_ns`，或这些字段与 manifest 不一致，执行端
会重新处理该 PDF，而不是把旧输出计为 skipped；输出审计会分别报告
`sidecar_input_missing` 或 `sidecar_input_mismatch`。如果成功 sidecar 指向预期
输出目录之外的 artifact，输出审计会报告带 `outside_output_dir` 原因的
`artifact_invalid`。如果成功 sidecar 的页级
摘要里仍有失败页，输出审计会报告 `page_failure`。失败 sidecar 会写入
`failure_category`，并在 parser 能识别时写入 `error_type`，便于 shard retry
排障时区分 timeout、网络、模型输出等失败类型。OCR 前的 manifest freshness
失败会使用 `InputMissing`、`InputChanged` 或 `InputInvalid` 作为 sidecar
`error_type`。
Agent 和 control 的失败分类会把负数 signal return code，以及 `137` 这类
shell 风格 `128 + signal` 退出码归为 `process_killed`，避免 OOM kill 或
SIGKILL 被误归到泛化的 `process_failed`。

manifest/shard 模式禁止使用 `--flatten_output`。输出目录必须保留
`relative_path` 的父目录，否则不同目录下的同名 PDF 会写入同一个输出位置，
破坏 shard 重跑和审计的幂等性。外部 manifest 也不能包含重复的
`relative_path`，否则两个输入 PDF 会映射到同一个输出 key，应在 OCR 启动前
拒绝。control 主机如果在注册时能读到 external manifest，会立即拒绝重复输出
key；如果是 control 本机不可见的远端路径，则仍需要保留 integrity/audit 检查。

如果任务使用 `distributed manifest scan`，控制端的 manifest integrity API 会把
每个已完成 scan unit 的 `manifest_path` 视为权威 manifest 分片：逐个校验 scan
unit manifest 行数、声明的 meta 文件和所有 shard 文件。此模式下顶层
`manifest_path` 可以是逻辑聚合路径，不要求控制端本机一定能看到一个已合并的
全局 `manifest.jsonl`。

Job summary API 和 UI 的 work plan 会直接展示扫描快照状态：
`scan_status` 表示实时扫描生命周期，`manifest_status` 表示 manifest 记录状态，
`manifest_snapshot_status` 会显示 `scanning`、`ready`、`frozen` 或 `missing`，
`shards_created` 是当前已生成的 shard 行数，`executable_shards` 是当前仍可执行
或可恢复的 pending/running/retrying/stale shard 数，`manifest_frozen_at` 在控制端确认所有 scan unit 已关闭、shard 计划已固定后填充。
`frozen` 只表示该 manifest 快照不会再追加 scan unit 或 shard；它不等于文件仍然
存在，也不等于 OCR 输出完整。生产任务仍要运行 manifest integrity 检查，并按
shard 抽检或全量审计输出产物。
分布式扫描冻结后，summary 还会复用已保存 freeze report 里的
`manifest_integrity_status`、`manifest_integrity_ok` 和
`manifest_integrity_issue_count`，让列表页无需重新扫描文件系统也能标出
manifest 或 shard 文件校验失败的 frozen 快照。

## Recovery 演练

正式生产前建议做一次受控 recovery 演练。演练时可以临时把
`OCR_SHARD_LEASE_SECONDS` 调小到 `60`，演练结束后恢复生产值，例如 `300`。

演练步骤：

1. 启动至少两个 agent。
2. 创建一个多 shard 的 distributed job。
3. 在某个 agent 正在运行 shard 时停掉它。
4. 等待 shard lease 过期。
5. 确认另一个 worker 重新领取该 shard，并且 `attempt_count` 增加。
6. 确认 job 根据 `max_shard_attempts` 最终成功或失败。

## 常见问题排查

Worker 没有出现在 UI：

- 检查 `OCR_CONTROL_URL`。
- 查看 `scripts/ocr_agent_worker.sh logs ...`。
- 确认控制端防火墙允许执行端访问。

Worker 出现了，但路径显示 blocked：

- 检查 `OCR_AGENT_SHARED_ROOTS`。
- 确认 UI 中填写的 `input_dir` 位于某个 shared root 下。
- 在执行端机器上检查共享盘读写权限。

执行机停掉后 shard 没有恢复：

- 检查 `OCR_SHARD_LEASE_SECONDS`。
- 确认还有其他 worker 在线且能访问输入路径。
- 确认 job 的 selected worker 范围包含接手机器。

Job 重试后失败：

- 检查 `max_shard_attempts`。
- 查看 job logs 和输出目录。
- 确认模型 endpoint、API key 和 parser 参数。

UI 无法 fetch API：

- 使用 `http://ocr-control.internal:8080/ui/`，不要使用
  `file://.../index.html`。
- 不要使用 `http://127.0.0.1:8080/ui/` 作为生产入口，除非浏览器就在控制端
  机器本机上运行；远端执行机和其他运维机器无法通过这个地址访问控制端。
- 确认控制端 API 进程正在运行。
- 确认 `OCR_PLATFORM_HOST` 不是只绑定到 `127.0.0.1`。
