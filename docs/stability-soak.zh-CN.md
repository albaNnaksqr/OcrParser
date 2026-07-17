# v0.3 稳定性试运行

[English](stability-soak.md) | 中文

本手册用于在隔离的 production-like 环境验证正式 wheel。只能使用公开或脱敏 PDF，
不得与生产部署共用数据库、服务、端口、spool 或输出目录。

## 拓扑与安全边界

- 一个任务专属 PostgreSQL 16 数据库；
- 一个从 release wheel 安装的 Control；
- 两个使用不同 server ID、work directory 和 spool directory 的 Agent；
- 一个任务专属 mock OCR endpoint，用于 24 小时 soak；
- 仅对任务专属 MinerU/Paddle 服务执行 outage，禁止停止共享模型服务；
- 所有运行凭据仅通过环境变量提供。

报告应写到 checkout 外。仓库也会忽略 `stability-artifacts/`，但仍建议使用绝对
scratch path。

## 前置门禁

第一个任务前，runner 会验证 release wheel 版本、不可变 revision、clean-build
provenance、无需鉴权的 `/source.json`、migration checksum 和 PostgreSQL 并发 shard
claim。v0.3.0 稳定性验证要求：

- 版本 `0.3.0`；
- revision `47e1c0399db97f4ec48715548b8c937bc77c20ba`；
- 运行中 Control 返回 `release_build=true`。

从公开 GitHub Release 下载 wheel，并安装到 Control/Agent runtime 环境。验证已发布
wheel 时，validation tools 使用单独 checkout。

production-like helper 可以生成所需的双 Agent 拓扑，同时保留原有单 Agent 默认行为：

```bash
python3 tools/local_prod_env.py --state-dir /scratch/ocr-soak/runtime up \
  --with-worker \
  --worker-count 2 \
  --with-mock-ocr \
  --shared-root /scratch/ocr-soak/shared
```

## Mock Soak

Control token 和独立测试数据库 URL 只放在环境中，不进入 argv 或报告：

```bash
export OCR_SOAK_CONTROL_TOKEN='set-at-runtime'
export OCR_SOAK_DATABASE_URL='postgresql+psycopg://user:password@127.0.0.1:15432/database'
```

使用两个 worker，在 24 小时内运行 20 个周期，每周期生成 100 个 PDF：

```bash
python3 tools/run_stability_soak.py \
  --wheel /scratch/releases/ocrparser_platform-0.3.0-py3-none-any.whl \
  --source-json-url http://127.0.0.1:38080/source.json \
  --database-url-env-var OCR_SOAK_DATABASE_URL \
  --control-url http://127.0.0.1:38080 \
  --control-token-env-var OCR_SOAK_CONTROL_TOKEN \
  --runtime-python /scratch/ocr-v030/bin/python \
  --runtime-repo-dir /scratch/ocrparser-v030 \
  --shared-root /scratch/ocr-soak/shared \
  --report-dir /scratch/ocr-soak/report \
  --worker-id soak-worker-01 \
  --worker-id soak-worker-02 \
  --engine-profile mock \
  --engine dotsocr \
  --ocr-host 127.0.0.1 \
  --ocr-port 18000 \
  --model-name mock-ocr \
  --cycles 20 \
  --duration-seconds 86400 \
  --documents-per-cycle 100
```

输入模式轮换为 `directory`、`existing_manifest` 和
`distributed_remote_folder_snapshot`。每个周期记录 job state、manifest integrity、
output audit、sidecar stage/fallback label、fault result 和资源采样。通过重复的
`--resource-pid-file LABEL=PATH` 增加进程资源采样。

## 故障 Hook

Fault hook 必须是 argv array，不能是 shell string。hook 在指定 cycle 运行期间执行，
并接收 `OCR_SOAK_CYCLE` 和 `OCR_SOAK_REPORT_DIR`。hook 只能操作任务专属 PID file、
tmux session、container 或 loopback port；恢复断言失败时必须返回非零。

```json
{
  "hooks": [
    {
      "name": "terminate-agent-02-and-wait-for-lease-reclaim",
      "cycle": 4,
      "after_seconds": 5,
      "argv": ["/scratch/ocr-soak/hooks/agent-reclaim"]
    },
    {
      "name": "control-outage-and-spool-replay",
      "cycle": 8,
      "after_seconds": 5,
      "argv": ["/scratch/ocr-soak/hooks/control-outage"]
    },
    {
      "name": "agent-shutdown-no-late-reporting",
      "cycle": 12,
      "after_seconds": 5,
      "argv": ["/scratch/ocr-soak/hooks/agent-shutdown"]
    }
  ]
}
```

通过 `--fault-plan` 传入。运维方提供的 hook 必须验证 lease recovery、spool replay
和无 late reporting，不能只发送 signal。

对于任务专属 MinerU/Paddle 服务，再增加一个 hook：停止模型服务 60 秒、按准确锁定
runtime 重启，并确认任务记录受控 retry/failure category，且不会产生 false success。

## 验收与证据

`report.json` 和 `report.md` 是权威结果。以下任一情况阻塞发布：

- migration/source/wheel/claim gate 失败；
- 超过两倍 lease window 后任务仍未进入终态；
- manifest 或 output audit 失败；
- claim、artifact 或 event 丢失/重复；
- fault hook 未执行或恢复断言失败；
- 出现未知 stage/fallback label；
- warm process RSS 或文件描述符增长超过 20%；
- 最后四分之一 mock 吞吐比最初四分之一下降超过 10%。

真实引擎每个使用 50 个公开页面，并发限制为 1-2。只记录当前固定部署证据，不与服务
版本或副本数不同的历史报告比较。

## 清理与回滚

停止任务专属 Agent、Control、mock/model 服务和 PostgreSQL。确认对应端口关闭、无任务
container 或 GPU process 残留，只保留脱敏报告。若发现 P0/P1 缺陷，继续把 v0.3.0
作为最新生产建议，将报告标为 release-blocking evidence，修复后再创建 v0.3.1 tag。
稳定化之后的方向记录在 [v0.4 运维成熟度 RFC](rfc-v0.4.zh-CN.md)。
