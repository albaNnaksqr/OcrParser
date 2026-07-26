# v0.3 稳定性试运行

[English](stability-soak.md) | 中文

本手册用于在隔离的 production-like 环境中验证冻结的 release wheel。它检查平台
稳定性，不是模型质量 benchmark。

## 拓扑与安全边界

- 使用任务专属 PostgreSQL 数据库、Control、mock OCR 服务，以及至少两个采用独立
  work/spool 目录的 Agent。
- 只使用公开或脱敏 fixture，不与生产环境共享数据库、服务、spool 或输出状态。
- 凭据只能通过指定环境变量传入，不得写入命令、报告或仓库。
- 报告和原始证据保存在 checkout 之外；只有脱敏摘要可以公开。

## 前置门禁

处理任务前必须确认：

- wheel 版本和不可变 source revision 与冻结候选一致；
- build provenance 表明这是 clean release build；
- `/source.json` 指向同一份源码；
- migration checksum 和 PostgreSQL 并发 claim 验证通过；
- runtime 仅使用任务专属目录和服务。

使用 `python3 tools/run_stability_soak.py --help` 查看 runner 的当前接口。调用时必须
提供候选 wheel 与 revision、runtime 位置、凭据环境变量名、不同 worker identity、
报告位置、workload 规模、周期数和配置时长。runner 只有在所有周期完成且达到配置的
完整时长后才会结束。

## 覆盖范围

轮换 directory、existing-manifest 和 distributed-snapshot 输入，覆盖 Agent 丢失与
重新领取、Control 暂时不可用后的 spool replay、Agent 优雅关闭与重启、migration
验证和输出审计。Fault hook 必须采用 argv array，并且只能操作任务专属资源。

真实引擎检查属于独立部署门禁。它只使用公开 fixture；需要中断时仅操作任务专属服务，
也不与 runtime 或副本数不同的历史部署比较吞吐。

## v0.3.1 验证结论

冻结候选已完成长期、多周期隔离稳定性验证。核心调度、恢复、replay、终态、manifest
和输出完整性检查通过，没有发现产品 P0/P1。

复核发现验证工具存在两个问题：所有周期完成后没有继续满足配置的完整时长，并且资源
门禁会对低基数波动过度敏感。这两项行为现已修正并补充回归测试。基于已完成证据的
覆盖范围和运行时长，项目接受剩余验证风险，不再重复完整长跑。该结论不得描述为严格
完整时长 soak 已完成。

详细证据仅在内部、仓库之外保存，并且不包含运行时凭据。

## 验收与证据

发布证据必须表明：

- source、wheel、migration 和 claim 门禁通过；
- 所有 Job 在有限时间内进入终态，并且没有 claim、artifact 或 event 丢失/重复；
- spool replay、重启恢复和 shutdown 行为正确；
- manifest 和输出审计通过；
- stage/fallback 分类保持已知且数量有界；
- warm process 资源保持有界，不存在持续上升趋势；
- 吞吐不存在实质性的持续回退；
- 清理后没有任务专属进程或服务继续运行。

如果发现产品 P0/P1，应将报告作为 release-blocking evidence 保存，并在创建 tag 前
修复。稳定化之后的方向记录在 [v0.4 运维成熟度 RFC](rfc-v0.4.zh-CN.md)。
