# v0.4.0rc2 候选版本

[English](release-v0.4.0rc2.md) | 中文

`0.4.0rc2` 取代 `0.4.0rc1`，保持相同的 v0.4 Control、认证、可观测性、
调度和兼容范围。唯一运行时改动是修复 RC 审查中发现的 Agent event/log spool
持久性竞态。

## 修复

此前 Agent replay 会读取待处理 JSONL 文件、等待 Control 请求，再用读取时的旧
快照替换文件。请求期间新追加的 event 或 log 可能因此在尚未到达 Control 时被
删除。

现在 event 和 log 分别串行执行 replay。成功或永久拒绝的记录会基于最新磁盘文件
进行确认删除，因此请求期间追加的其他记录仍会持久保留。网络请求期间不会持有文件锁。

## 兼容性

本候选不修改 CLI、HTTP/OpenAPI、数据库 migration、event/log payload、spool
JSONL 格式、状态值、Agent lane、Parser 行为、manifest/output 格式或模型集成。
Schema 上限仍为 migration `0020`。

## 候选完整性

发布 wheel 必须从 `v0.4.0rc2` tag 指向的 clean commit 构建。安装后的
distribution 和 `/source.json` 必须报告版本 `0.4.0rc2`、准确 source revision、
`dirty=false` 和 `release_build=true`。

候选需要通过完整自动化矩阵和一次短时隔离 Control 中断/replay 验证。不需要启动
GPU 服务，也不需要重新执行四小时 soak。正式 `v0.4.0` 仍需完成 RC 观察期，期间
不得再有 tracked runtime 改动。
