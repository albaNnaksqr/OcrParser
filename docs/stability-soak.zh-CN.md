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
claim。v0.3.1 发布候选必须显式传入：

- 版本 `0.3.1`；
- 通过 `--expected-revision` 传入准确冻结候选 commit；
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
export OCR_SOAK_EXPECTED_REVISION='set-to-the-frozen-v0.3.1-commit'
```

使用两个 worker，在 24 小时内运行 20 个周期，每周期生成 100 个 PDF：

```bash
python3 tools/run_stability_soak.py \
  --wheel /scratch/releases/ocrparser_platform-0.3.1-py3-none-any.whl \
  --expected-version 0.3.1 \
  --expected-revision "$OCR_SOAK_EXPECTED_REVISION" \
  --source-json-url http://127.0.0.1:38080/source.json \
  --database-url-env-var OCR_SOAK_DATABASE_URL \
  --control-url http://127.0.0.1:38080 \
  --control-token-env-var OCR_SOAK_CONTROL_TOKEN \
  --runtime-python /scratch/ocr-v031/bin/python \
  --runtime-repo-dir /scratch/ocrparser-v031 \
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

## v0.3.1 Wave 5 短预演证据

revision `12abb795aa55f986cea29aa0e24451be40bd6f77` 的第一次短运行正确失败，
暴露同 server stale reclaim starvation P1。eligible→claim 为 49.862 秒，
eligible→Job terminal 为 57.436 秒。失败已审计并清理，24 小时 soak 没有启动。
保留的 summary hash：

- `report.json`：`9d4387207b37f649ee2c48abe1f509c42188883560d5429be7224e3565f1fdbe`
- `report.md`：`cb9e0a999b3f26bd404c270cc46257bcbe3844a3c19fd50ed443d08612a4fa1e`

revision `1d3c8f560e94c4550718fc9910e8344ef38eae89` 在同 server 注册时 fence
之前的 running shard/current attempt 与 previous running scan unit，清除 owner/lease，并让
stale/retrying claim 优先于 pending work；公开接口保持不变。GitHub CI run
`29895536565` 的 11/11 Job 和 846 个测试通过。复验 clean wheel SHA256 为
`33482a9265f68b9be0b7b9bebc8b845bc9d5e6443b9a4c606c844f70f2c838d3`。

一次修复后运行过早触发 fault。fence 与 claim 断言通过，但仍有 7 个普通 pending
shard，因此 Job 用 48 秒完成。原始结果继续保持 **FAIL**。summary hash：

- JSON：`051f14d8d6352f996564e6f04129fd1507871b8cd84e54cf74d1d16fff4dfb01`
- Markdown：`9e4e8a6c16a8442b86f984232e2c899f10641c24e80d0a8b7566bd49dde0dfa4`

随后全新 strict r4 在重启时仍有准确两个普通 pending shard 的条件下运行，3/3
周期通过：

- Cycle 1 termination/fault injection→attempt 2 claim 为 19.327 秒；
- Cycle 2 replay 30 条 event/log 和 1 条 shard update，migration `plan`、`apply`、
  `verify` 通过；
- Cycle 3 register fence 为 0.095 秒，eligible→claim 为 0.550 秒，
  eligible→target shard terminal 为 1.725 秒，stale 先于 pending，Job 在 18.487 秒进入终态；
- 300/300 份文档和 30/30 个 shard 成功；duplicate 为 0，spool/quarantine 为空，
  manifest/output audit 为 100%，resource 通过，cleanup 残留为 0。

成功运行 SHA256：

- `report.json`：`107a8d18edca13bd5c8458c12f5e73b631c3468ef0ae066a83dfe34619e7fcce`
- `report.md`：`a0d4d98051aae0d0d60aadf169a182e8597204f2eea388ddff5f1949e06a3959`
- operator timeline：`671edca29d5bc89843baae42cc3b291e393c339ed0f4d7fd50b8bbacf68f82f7`
- audit：`422e5359cbade42e930f05885ae1db7f41aa6091f77d4a0863c25c514ed45848`
- cleanup：`e93b0ac4a63bdf17998b60deceaf3d0b00988e21ee61ddecffc0f34bde597e53`

以上仍只是短预演，不能替代准确最终候选的 24 小时运行；24 小时运行尚未启动。
详见 [v0.3.1 发布说明](release-v0.3.1.zh-CN.md)。

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
