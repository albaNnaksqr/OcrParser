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
- 同 server 注册现在会同时 fence 该 worker 之前的 running shard/current attempt 和
  previous running scan work，并清除 owner/lease，使其可被重新领取。claim 路径优先处理
  stale/retrying work，避免普通 pending backlog 饿死恢复任务。

基础安装同时声明 `beautifulsoup4>=4.12,<5`。PaddleOCR-VL 多页表格合并会导入该
依赖；原问题是安装依赖缺失，不是 OCR 算法调整。

## 稳定性证据

最初的隔离短预演发现同 server stale reclaim starvation。上文的 register fence 和
stale/retrying claim 优先级在不改变公开接口的前提下修复了该恢复路径。新的短预演随后
通过 reclaim、replay、migration、输出审计和清理检查。

冻结候选之后完成了覆盖多种输入模式的长期、多周期隔离稳定性验证。核心调度、恢复、
spool replay、终态、manifest 和输出完整性检查通过，没有发现产品 P0/P1。

复核发现验证工具在所有周期完成后没有继续保证配置的完整时长，并且会对低基数资源波动
过度敏感。这两项工具行为现已修正并补充回归测试。基于已经获得的证据，项目接受剩余
验证风险，不再重复完整长跑。因此本版本不宣称严格完整时长 soak 已完成。详细报告仅在
内部、仓库之外保存，并且不包含运行时凭据。

## 真实引擎状态

三个引擎均保持 **Verified（已验证）**，没有标为 **Certified（已认证）**：

- DotsOCR 完成集成检查，但托管服务未暴露不可变模型或 runtime 来源。
- MinerU 完成集成与任务专属恢复检查，但 reading-order 质量和不可变 runtime 打包限制
  仍不满足认证条件。
- PaddleOCR-VL 在补齐基础依赖后完成集成与任务专属恢复检查，但质量、不可变镜像来源和
  已记录的 FlashInfer 前置条件仍是认证限制。

详细 fixture 矩阵、revision、digest 和限制见[引擎认证](engine-certification.zh-CN.md)。
模型质量缺失只记录为限制，不能表示为 Parser 已认证成功。

## 发布门禁

创建 tag 前必须从最终 commit 构建 clean wheel，并验证：

- Python 3.10、3.11、3.12 测试和 GitHub CI；
- base、`platform`、`s3`、`layout`、`full` 安装 profile；
- 四个 console script、UI/package data、migration checksum 和本地文档链接；
- wheel source revision、`dirty=false`、`release_build=true`；
- AGPL `/source` 与 `/source.json` 指向准确 tag 源码；
- 最终候选的短预演和长期验证证据已经按上述风险接受结论完成复核。

发布本身不得启动共享 GPU 服务。真实引擎证据是独立部署门禁；outage 测试只操作
任务专属服务。
