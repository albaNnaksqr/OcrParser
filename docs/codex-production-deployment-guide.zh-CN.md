# Codex 生产部署安装引导

本文档给 Codex 或其他自动化部署助手使用。目标是在真实生产环境中部署 OCR
Platform 控制端 UI 和远端 agent。部署前不要一厢情愿地假设主机、路径、端
口、数据库、共享盘或模型服务配置；能通过免密 SSH 查看就先查看，不能查看
就向用户确认。

## 基本原则

- 默认控制端、执行端、共享盘、模型 API 是分离的。
- 默认控制端和执行端之间已经配置免密 SSH，但仍需实际验证。
- 不要把测试机器名、个人路径、临时 API key 写进生产配置。
- 不要在回答或日志中暴露 API key、数据库密码、私有 PDF 路径清单。
- 不要使用破坏性命令清理生产目录，除非用户明确授权。
- 生产数据库应使用 PostgreSQL；SQLite 只用于本机开发或临时验证。
- 远端 agent 退出后，控制端不能直接把它启动回来；生产自恢复依赖
  systemd、Kubernetes 或其他本机 supervisor。

## 部署前必须确认的信息

如果用户没有提供，先通过 SSH 或询问补齐：

```text
control_host: 控制端主机 SSH 名称或 IP
agent_hosts: 执行端主机 SSH 名称或 IP 列表
release_ref: release tag 或 commit
control_deploy_dir: 控制端代码部署目录
agent_deploy_dir: 执行端代码部署目录
control_url: 执行端可访问的控制端 HTTP URL，必须是内网地址，不能是 127.0.0.1
postgres_dsn: PostgreSQL 连接串或由用户创建好的库/账号
shared_roots: 所有执行端共同可见的共享盘根路径
input_dir: 生产输入 PDF 目录
output_dir: 生产输出目录
manifest_root: manifest/shard 文件目录
model_profiles: DotsOCR、MinerU、PaddleOCR-VL 的生产 LB 地址和模型名
api_key_source: API key 从 UI 输入、环境变量、密钥服务还是配置文件获得
worker_count_per_host: 每台执行机启动几个 worker
```

如果能 SSH 到机器，优先执行只读检查：

```bash
ssh <host> 'hostname; whoami; date; nproc; free -h; df -h / /opt /mnt 2>/dev/null || df -h'
ssh <host> 'command -v git python3 pip systemctl tmux curl || true'
ssh <host> 'mount | head -50'
```

共享盘检查需要在每台执行端执行：

```bash
ssh <agent_host> 'for p in /shared/ocr-data /shared/ocr-data/manifests; do echo "== $p =="; test -d "$p" && test -r "$p" && echo readable; test -w "$p" && echo writable; done'
```

把 `/shared/ocr-data` 替换成用户提供或 SSH 查到的真实共享盘路径。

## 控制端安装流程

1. 登录控制端。
2. 创建运行账号和部署目录。
3. 拉取指定 release。
4. 安装 Python venv 和依赖。
5. 配置 PostgreSQL DSN。
6. 启动 control API/UI。
7. 验证 `/api/servers` 和 `/ui/`。

参考命令模板：

```bash
sudo useradd --system --create-home --home-dir /var/lib/ocr-control --shell /bin/bash ocr-control
sudo mkdir -p /opt/ocr-platform /etc/ocr-platform /var/log/ocr-platform
sudo chown -R ocr-control:ocr-control /opt/ocr-platform /var/log/ocr-platform

sudo -iu ocr-control
cd /opt/ocr-platform
git clone https://github.com/YOUR_ORG/ocrparser ocrparser
cd ocrparser
git checkout <release_ref>
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

首次生产部署时，在 PostgreSQL 库和账号创建完成后，按文件名顺序应用控制端
所有 SQL migration：

```bash
python tools/apply_control_migrations.py \
  --database-url "$OCR_PLATFORM_DATABASE_URL"
```

后续 schema 变化应追加新的 `ocr_platform/control/migrations/*.sql` 文件。不要
把生产数据库长期维护只交给应用启动时的兼容升级逻辑。

部署前应确认用户已有 PostgreSQL 和 `manifest_root` 的备份策略。详细操作见
`docs/ocr-platform-backup-restore.zh-CN.md`；不要只备份 PG，也不要只备份
共享盘 manifest/shard 文件。

生产灰度前，执行 PostgreSQL shard claim 并发验证：

```bash
python tools/pg_claim_stress.py \
  --database-url "$OCR_PLATFORM_DATABASE_URL" \
  --shards 1000 \
  --scan-units 1000 \
  --scan-unit-shards 2 \
  --workers 64 \
  --json
```

如果目标库是空的临时测试库，可以加 `--apply-init-db`。正式生产库应先应用 SQL
migration。验收标准：`ok=true`、`duplicate_claims={}`、`missing_claims=0`、
`attempt_conflict_rejected=true`、`scan_unit_claims.ok=true`，并且
`scan_unit_completion_shards.ok=true`，证明并发完成分布式 scan unit 时生成的
全局 shard index 连续且无重复。

创建 `/etc/ocr-platform/control.env`：

```bash
OCR_PLATFORM_DATABASE_URL=postgresql+psycopg://<user>:<password>@<postgres-host>:5432/<db>
OCR_PLATFORM_HOST=0.0.0.0
OCR_PLATFORM_PORT=8080
OCR_PLATFORM_API_TOKEN=<long-random-control-api-token>
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

千万级任务建议把 `OCR_JOB_FILE_DETAIL_LIMIT`、`OCR_JOB_EVENT_DETAIL_LIMIT`
和 `OCR_JOB_LOG_DETAIL_LIMIT` 调小；如果不需要 recent files、原始事件或
DB 内 stdout/stderr 排障视图，可以设为 `0`。设为 `0` 后控制端不再写入对应
明细表，Jobs summary 仍会使用聚合计数、shard 计数和 manifest 计数展示总体
进度；`OCR_JOB_FAILED_FILE_SAMPLE_LIMIT` 和
`OCR_JOB_RECENT_ERROR_SAMPLE_LIMIT` 会继续限制 counter 中保留的失败文件与
job/shard 级错误样本数量。

生产控制端不要只监听 `127.0.0.1`。`OCR_PLATFORM_HOST=0.0.0.0` 表示服务监听
控制端机器的所有网卡，再由防火墙、安全组或反向代理限制可信内网访问。后
续给 agent 使用的 `OCR_CONTROL_URL` 也必须是执行端能访问到的内网地址，例
如 `http://<control-host>:8080` 或 `http://10.x.x.x:8080`，不能写
`http://127.0.0.1:8080`。

生产环境建议必须设置 `OCR_PLATFORM_API_TOKEN`。设置后，所有 `/api/` 请求都
需要带 `Authorization: Bearer <token>` 或 `X-OCR-Platform-Token: <token>`。
UI 静态页面仍可打开，但浏览器访问 API、agent 注册、heartbeat、claim shard
都必须使用这个 token。这个 token 应只放在控制端和执行端的 root/服务账号可
读配置文件中，不要提交到 git 或写入普通日志。

启动后验证：

```bash
curl -H 'X-OCR-Platform-Token: <long-random-control-api-token>' http://<control-host>:8080/api/servers | python3 -m json.tool
curl -H 'X-OCR-Platform-Token: <long-random-control-api-token>' http://<control-host>:8080/api/jobs/summary | python3 -m json.tool
```

## 执行端 Agent 安装流程

每台执行端都需要安装代码和 agent。多台执行端可以使用相同 release，但
`OCR_AGENT_SERVER_ID` 必须全局唯一。

生产共享盘权限必须通过统一运行组管理，而不是依赖当前登录用户。所有挂载
共享盘的机器应使用一致的 UID/GID，或通过 LDAP/SSSD 统一身份：

```bash
sudo groupadd --system --gid 2400 ocr-runtime
sudo groupadd --system --gid 2402 ocr-agent
sudo useradd --system --uid 2402 --gid ocr-agent --groups ocr-runtime \
  --create-home --home-dir /var/lib/ocr-agent --shell /bin/bash ocr-agent
sudo usermod -aG ocr-runtime "$USER"

sudo mkdir -p /shared/ocr-data/ocr-platform/{manifests,jobs}
sudo chown -R root:ocr-runtime /shared/ocr-data/ocr-platform
sudo chmod 2775 /shared/ocr-data/ocr-platform
sudo find /shared/ocr-data/ocr-platform -type d -exec chmod 2775 {} +

findmnt /shared/ocr-data
sudo -u ocr-agent test -w /shared/ocr-data/ocr-platform
sudo -u ocr-platform test -w /shared/ocr-data/ocr-platform
```

生产服务变更前，先运行只读巡检工具确认 control、worker 和共享盘状态。该
工具只通过 SSH 做读取检查，不会写远端文件或重启服务：

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

参考命令模板：

```bash
sudo mkdir -p /opt/ocr-platform /etc/ocr-agent /var/lib/ocr-agent /var/log/ocr-agent
sudo chown -R ocr-agent:ocr-agent /opt/ocr-platform /var/lib/ocr-agent /var/log/ocr-agent

sudo -iu ocr-agent
cd /opt/ocr-platform
git clone https://github.com/YOUR_ORG/ocrparser ocrparser
cd ocrparser
git checkout <release_ref>
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

单 worker 配置文件示例 `/etc/ocr-agent/worker.env`：

```bash
OCR_AGENT_SERVER_ID=<unique-worker-id>
OCR_CONTROL_URL=http://<control-host>:8080
OCR_CONTROL_API_TOKEN=<long-random-control-api-token>
OCR_REPO_DIR=/opt/ocr-platform/ocrparser
OCR_AGENT_WORK_DIR=/var/lib/ocr-agent/<unique-worker-id>
OCR_AGENT_PYTHON=/opt/ocr-platform/ocrparser/.venv/bin/python
OCR_AGENT_SHARED_ROOTS=/shared/ocr-data
OCR_AGENT_POLL_INTERVAL=2
OCR_AGENT_HEARTBEAT_INTERVAL=5
OCR_AGENT_CONTROL_RETRY_INITIAL=1
OCR_AGENT_CONTROL_RETRY_MAX=30
OCR_AGENT_EVENT_SPOOL_DIR=/var/lib/ocr-agent/<unique-worker-id>/event-spool
OCR_AGENT_EVENT_SPOOL_MAX_MB=1024
OCR_AGENT_TERMINATION_TIMEOUT=10
OCR_AGENT_STOP_POLL_INTERVAL=1
OCR_MANIFEST_SCAN_PROGRESS_INTERVAL_FILES=10000
OCR_AGENT_RUNNER=tmux
OCR_AGENT_LOG_DIR=/var/log/ocr-agent/<unique-worker-id>
OCR_AGENT_TMUX_SESSION=ocr-agent-<unique-worker-id>
OCR_AGENT_GIT_REF=<release_ref>
OCR_AGENT_SCRIPT_VERSION=ocr-agent-worker-v1
```

启动验证：

```bash
cd /opt/ocr-platform/ocrparser
scripts/ocr_agent_worker.sh doctor /etc/ocr-agent/worker.env
scripts/ocr_agent_worker.sh start /etc/ocr-agent/worker.env
scripts/ocr_agent_worker.sh status /etc/ocr-agent/worker.env
```

## 单机多 Worker 安装流程

当执行机较少、机器资源充足、远端模型 API 没打满时，可以一台机器启动多
个 worker。每个 worker 必须使用独立配置文件。

示例：一台 96 核、376 GiB 内存机器先启动 4 个 worker。

```bash
sudo mkdir -p /etc/ocr-agent

for i in 1 2 3 4; do
  sudo cp /etc/ocr-agent/worker.env /etc/ocr-agent/worker-${i}.env
  sudo sed -i "s/^OCR_AGENT_SERVER_ID=.*/OCR_AGENT_SERVER_ID=<host-name>-worker-${i}/" /etc/ocr-agent/worker-${i}.env
  sudo sed -i "s#^OCR_AGENT_WORK_DIR=.*#OCR_AGENT_WORK_DIR=/var/lib/ocr-agent/worker-${i}#" /etc/ocr-agent/worker-${i}.env
  sudo sed -i "s#^OCR_AGENT_LOG_DIR=.*#OCR_AGENT_LOG_DIR=/var/log/ocr-agent/worker-${i}#" /etc/ocr-agent/worker-${i}.env
  sudo sed -i "s/^OCR_AGENT_TMUX_SESSION=.*/OCR_AGENT_TMUX_SESSION=ocr-agent-worker-${i}/" /etc/ocr-agent/worker-${i}.env
  sudo chmod 0640 /etc/ocr-agent/worker-${i}.env
done
```

启动：

```bash
cd /opt/ocr-platform/ocrparser
for i in 1 2 3 4; do
  scripts/ocr_agent_worker.sh doctor /etc/ocr-agent/worker-${i}.env
  scripts/ocr_agent_worker.sh start /etc/ocr-agent/worker-${i}.env
done
```

systemd 实例化托管：

```bash
sudo cp services/ocr-agent-worker.service.example /etc/systemd/system/ocr-agent-worker@.service
sudo sed -i 's#/etc/ocr-agent/worker.env#/etc/ocr-agent/worker-%i.env#g' /etc/systemd/system/ocr-agent-worker@.service
sudo systemctl daemon-reload

for i in 1 2 3 4; do
  sudo systemctl enable --now ocr-agent-worker@${i}
done
```

## 参数建议

普通单 worker 初始基线：

```text
target_files_per_shard: 1000-5000
page_concurrency: 80
file_concurrency: 8
num_cpu_workers: 56
max_shard_attempts: 3
OCR_SHARD_LEASE_SECONDS: 300
OCR_SCAN_UNIT_CLAIM_BATCH_SIZE: 100
```

96 核、376 GiB 内存执行机的单机多 worker 阶梯：

```text
initial:
  worker_count_per_host: 4
  page_concurrency_per_worker: 120
  file_concurrency_per_worker: 8
  num_cpu_workers_per_worker: 20-24

aggressive:
  worker_count_per_host: 4
  page_concurrency_per_worker: 160
  file_concurrency_per_worker: 8
  num_cpu_workers_per_worker: 20-24

scale_out:
  worker_count_per_host: 6
  page_concurrency_per_worker: 120-160
  file_concurrency_per_worker: 8
  num_cpu_workers_per_worker: 14-16
```

不要把 `num_cpu_workers=56` 同时应用到同一台机器上的每个 worker。单机多
worker 时，`page_concurrency`、`file_concurrency`、`num_cpu_workers` 都是
每个 worker 的参数，总资源消耗约等于单 worker 参数乘以 worker 数。

## 上线验证清单

控制端：

```bash
curl http://<control-host>:8080/api/servers | python3 -m json.tool
curl http://<control-host>:8080/api/jobs/summary | python3 -m json.tool
```

执行端：

```bash
cd /opt/ocr-platform/ocrparser
scripts/ocr_agent_worker.sh status /etc/ocr-agent/worker.env
scripts/ocr_agent_worker.sh logs /etc/ocr-agent/worker.env
```

UI 中需要看到：

- 所有预期 worker 在线，没有 stale。
- `git_ref`、`script_version`、`python_path` 符合本次发布。
- shared path 检查为 `ok`。
- CPU、内存、磁盘 pressure 为 ready。
- 小规模 distributed job 能被多个 worker 分别领取 shard。

灰度测试建议：

```text
stage_1: 100 PDF, selected workers 1-2 个
stage_2: 1000 PDF, selected workers 4 个
stage_3: 生产子目录小批次, all eligible workers
stage_4: 完整生产目录
```

观察指标：

```text
total_page_per_second
files_per_minute
api_inflight / api_limit / oldest_api_inflight
api_error_categories
api_error_status_codes
api_avg_latency
worker CPU / memory / disk pressure
shard retry / stale / failed / stopped
```

如果 `api_error_categories.timeout/network/http_status/rate_limit` 上升，优先检
查模型服务、LB、网络和 API key。如果主要是 `model_output` 上升，优先降低
每 worker 的 `page_concurrency` 或 worker 数，观察 malformed output 是否
下降。

## 需要询问用户的情况

以下信息不能靠猜：

- 生产 PostgreSQL DSN 或由谁创建数据库账号。
- 模型服务真实内网 LB 地址、端口、模型名、API key 获取方式。
- 共享盘真实挂载路径和权限。
- 控制端内网 URL，执行端是否能访问；不要默认使用 `127.0.0.1`。
- 是否允许使用 systemd，以及是否有 sudo 权限。
- 每台执行机计划启动几个 worker。
- 是否允许对生产目录做写入测试。
- 是否允许启动灰度 OCR job。

如果缺少这些信息，先问用户；如果有 SSH 权限，先做只读检查，再给出需要用
户确认的最小问题列表。
