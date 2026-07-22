# v0.3.1 稳定性维护版本

[English](release-v0.3.1.md) | 中文

v0.3.1 只处理恢复、打包和部署证据，不改变 CLI、HTTP API、数据库 schema 与
migration 历史、manifest wire format、输出目录、Job/Shard 状态词汇或 Python
兼容 façade。

## 恢复修复

- 已分配给 worker 的 Job 可以继续领取剩余 static shard，不再处理第一个 shard 后
  提前停止。
- Worker 的 shard update 先持久写入、幂等 replay；Control 暂时中断后，迟到 update
  不能把 terminal shard 回退为非终态。
- Work lease 只对仍在运行的 Job 续期；已停止或不活跃的 Job 不会让 stale scan/shard
  无限保持 lease。

基础安装同时声明 `beautifulsoup4>=4.12,<5`。PaddleOCR-VL 多页表格合并会导入该
依赖；原问题是安装依赖缺失，不是 OCR 算法调整。

## 脱敏发布前证据

revision `e31e494a721a23c9103ccf3f79646575ae2d468c` 的 clean `0.3.0` 候选在
隔离 Spark 环境使用 PostgreSQL 16 和两个 Agent 完成三周期短预演：

- 300/300 份生成公开 PDF、30/30 个 shard 完成；
- Cycle 1 强制终止一个 Agent，另一 Agent 以 attempt 2 接管；
- Cycle 2 停止 Control 60 秒，随后 replay 36 条 event/log 和 1 条 shard update，
  migration `plan`、`apply`、`verify` 通过；
- Cycle 3 执行同 server 的 Agent 优雅退出与重启，无非预期重复、quarantine 或退出后
  上报；
- 三个周期的 manifest/output audit 全部通过，清理后没有任务进程、端口、容器或 GPU
  process 残留。

Cycle 3 runner 最初从 restart request 起算终态时间，结果为 31.524 秒，比两倍 lease
阈值超出 1.524 秒。旧 lease 到期前不能重新领取；从 recovery eligible 时刻起算，
终态用时为 25.551 秒并通过。文档保留两种测量，不把原始结果改写为无条件通过。

脱敏证据校验值：

- `report.json`：`e5ba4e6300ba6befc5785926043915c7d5df0769ec17f2ca8fd1a92a403601ac`
- `report.md`：`ec15bbf44cbcc200bb06e8c027aecf557d43c6424275401aa4d9d7a252976e0c`
- `SHA256SUMS`：`ec4e32f094be63d7771f02c41f64ec4f2254f10fce20421800ae31c1328020fe`

以上是短预演，不是 v0.3.1 最终 24 小时 soak。24 小时运行必须使用最终 tag 对应的
准确 commit 和 wheel；任何 tracked-file 修改都会使结果失效。

## 真实引擎状态

三个引擎均保持 **Verified（已验证）**，没有标为 **Certified（已认证）**：

- DotsOCR：公开数据 50/50 页完成，质量 fixture 通过 3/4；托管服务未暴露不可变模型
  或 runtime 来源。
- MinerU：任务专属 outage 场景 50/50 页完成，质量 fixture 通过 3/4；剩余失败是模型
  reading-order 质量，并且派生 runtime 依赖没有固化在自己的不可变镜像中。
- PaddleOCR-VL：任务专属 outage 场景 50/50 页完成。`55d23996997508b61828d254e95aed8bf65d9752`
  候选在补齐基础依赖后完成四份 fixture 的集成，但质量仅通过 1/4；没有不可变
  RepoDigest，固定 base 组合还需要明确记录的 FlashInfer 版本检查 bypass。

revision、digest、fixture 结果和限制详见[引擎认证](engine-certification.zh-CN.md)。
模型质量缺失只记录为限制，不能表示为 Parser 已认证成功。

## 发布门禁

创建 tag 前必须从最终 commit 构建 clean wheel，并验证：

- Python 3.10、3.11、3.12 测试和 GitHub CI；
- base、`platform`、`s3`、`layout`、`full` 安装 profile；
- 四个 console script、UI/package data、migration checksum 和本地文档链接；
- wheel source revision、`dirty=false`、`release_build=true`；
- AGPL `/source` 与 `/source.json` 指向准确 tag 源码；
- 最终候选的三周期短预演和 24 小时 soak 通过。

发布本身不得启动共享 GPU 服务。真实引擎证据是独立部署门禁；outage 测试只操作
任务专属服务。
