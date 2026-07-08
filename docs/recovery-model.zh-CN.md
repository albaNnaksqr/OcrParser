# 恢复模型

[English](recovery-model.md) | 中文

OcrParser 将恢复分为本地 parser recovery 和分布式 platform recovery。
本地恢复避免在单机内重复昂贵工作；platform recovery 则让长时间运行的共享存储任务
在 worker 或网络路径失败时仍然可观察、可重新领取。

## 本地 Parser Recovery

Parser 会为已完成文件写出 output artifacts 和 status sidecars。
重新处理前，它可以检查预期产物是否已经存在，以及是否完整到足以信任。

这能保护常见场景：

- 一部分 PDF 完成后进程被中断；
- 使用同一 output directory 重启目录任务；
- manifest shard 被重试时，不应覆盖有效的已完成文件；
- partial output 不应被当作成功。

对于干净 benchmark runs，可以关闭 resume 并强制重新处理。
对于生产 runs，除非你明确想重新生成所有输出，否则应保持 resume 行为开启。

## Manifest Snapshot

分布式任务使用 manifest 让 folder scan 显式化：

1. Control plane 记录请求的 input/output paths。
2. Scan unit 列出可见 PDF 并写出 manifest。
3. Manifest 被拆成 shards。
4. Workers 领取 shards，并用 shard-specific input 调用 parser。

这个设计避免每个 worker 独立重新发现一个正在变化的目录树。
Manifest 就是任务的执行快照。

## Manifest Freeze And Integrity

**Manifest freeze** 记录 job 应该执行的 snapshot：file count、byte count、
shard count，以及 scan/shard state。冻结后的 manifest 是 job 的执行契约。

**Manifest integrity** 检查 control-plane metadata 和 manifest files 是否仍然一致。
它适合发现这些问题：

- shared storage 中 manifest file 缺失；
- shard files 缺失；
- shard file counts 与 manifest count 不匹配；
- failed scan 或手工文件操作后 metadata count 不匹配。

在多主机部署中，control server 只能验证它能读取的文件。
如果 worker 能看到某个 shared path，但 control server 没有挂载该路径，
integrity 可能报告文件缺失，即使 worker 仍然能读取它们。
为了让 integrity view 最有用，请在 control host 和 workers 上挂载同一共享存储。

## Shard Leases And Reclaim

Worker 领取 shard 时，control plane 会记录 assignment 和 lease metadata。
Heartbeats 会续租活跃工作。如果 worker 消失且 lease 过期，其他符合条件的 worker
可以重新领取该 shard。

这能保护常见 platform failures：

- worker process exit；
- host reboot；
- network interruption；
- parser subprocess 在发送 terminal shard update 前终止。

Platform 会跟踪 attempts，让 stale work 可见，而不是悄悄覆盖当前 shard state。

## Worker Update Spool

当 control API 暂时不可用时，workers 仍可能需要上报 shard progress 或 terminal state。
Update records 可以先写到本地 spool，之后再重放。

Malformed 或永久被拒绝的 records 会被隔离，因此一个坏 update 不会阻塞同一个 worker
后续有效 updates。

## Stop Semantics

停止 job 是协作式的：

- unclaimed shards 被标记为 stopped；
- running shards 被要求收敛；
- expired running leases 可以通过同一 stale path finalize；
- terminal job summaries 在 shard state 稳定后计算。

这样可以避免把一个分布式 job 假装成立即停止，实际上 worker 可能还在完成或上报当前 shard。

## 运维 Checklist

- 将 input、output 和 manifest paths 放在所有参与 workers 都可见的存储上。
- 如果希望 UI 提供完整 manifest integrity checks，请在 control host 也挂载同一共享存储。
- 保持 worker `server_id` 唯一。
- 小 shard size 有利于故障隔离；大 shard size 有利于降低调度开销。
- Lease timeout 应长于正常 shard heartbeat 间隔，但也要足够短，以便恢复 abandoned work。
