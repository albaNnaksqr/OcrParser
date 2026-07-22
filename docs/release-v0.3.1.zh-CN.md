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

## 脱敏 Wave 5 证据

revision `12abb795aa55f986cea29aa0e24451be40bd6f77` 的第一次短预演发现真实 P1：
同 server 重启后 stale work 虽然已可重新领取，却会被普通 pending shard 饿死。
eligible→claim 为 49.862 秒，eligible→Job terminal 为 57.436 秒。因此没有启动
24 小时 soak。

保留的 P1 failure summary SHA256：`report.json` 为
`9d4387207b37f649ee2c48abe1f509c42188883560d5429be7224e3565f1fdbe`，
`report.md` 为
`cb9e0a999b3f26bd404c270cc46257bcbe3844a3c19fd50ed443d08612a4fa1e`。

revision `1d3c8f560e94c4550718fc9910e8344ef38eae89` 通过上面的 register fence 和
stale/retrying claim 优先级修复该恢复路径，没有改变任何公开接口。GitHub CI run
`29895536565` 的 11 个 Job 全部通过，其中包含 846 个测试。其 clean `0.3.1` wheel
SHA256 为 `33482a9265f68b9be0b7b9bebc8b845bc9d5e6443b9a4c606c844f70f2c838d3`。

一次修复后的运行过早触发 restart fault。核心 fence 和 reclaim 断言通过，但当时仍有
7 个正常 pending shard，Job 合理地用了 48 秒完成。该运行保留原始 **FAIL**，不会
重新标记为 pass。

保留的过早 timing failure summary SHA256：JSON 为
`051f14d8d6352f996564e6f04129fd1507871b8cd84e54cf74d1d16fff4dfb01`，
Markdown 为
`9e4e8a6c16a8442b86f984232e2c899f10641c24e80d0a8b7566bd49dde0dfa4`。

随后全新 strict r4 在重启时仍有准确两个普通 pending shard 的条件下运行，三个周期
全部完成：

- Cycle 1 在 19.327 秒内接管被终止 Agent 的工作；
- Cycle 2 在 Control outage 后 replay 30 条 event/log 和 1 条 shard update，
  migration `plan`、`apply`、`verify` 通过；
- Cycle 3 在 0.095 秒内 fence 旧 assignment，eligible 后 0.550 秒领取 stale shard，
  eligible→target shard terminal 为 1.725 秒，stale work 先于 pending work，被测 Job 在 18.487 秒内
  完成；
- 300/300 份文档、30/30 个 shard 完成；duplicate 为 0，spool/quarantine 为空，
  manifest/output audit 为 100%，resource check 通过，cleanup 残留为 0。

脱敏成功运行校验值：

- `report.json`：`107a8d18edca13bd5c8458c12f5e73b631c3468ef0ae066a83dfe34619e7fcce`
- `report.md`：`a0d4d98051aae0d0d60aadf169a182e8597204f2eea388ddff5f1949e06a3959`
- operator timeline：`671edca29d5bc89843baae42cc3b291e393c339ed0f4d7fd50b8bbacf68f82f7`
- audit：`422e5359cbade42e930f05885ae1db7f41aa6091f77d4a0863c25c514ed45848`
- cleanup：`e93b0ac4a63bdf17998b60deceaf3d0b00988e21ee61ddecffc0f34bde597e53`

以上仍只是短预演，不是 v0.3.1 最终 24 小时 soak。24 小时运行必须使用最终候选的
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
