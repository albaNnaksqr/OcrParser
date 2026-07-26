# RFC：OcrParser v0.4 Control 内部重构

[English](rfc-v0.4-control-internals.md) | 中文

状态：Draft。v0.3.1 观察期内仅允许编写文档和评审。

本 RFC 从属于 [v0.4 运维成熟度 RFC](rfc-v0.4.zh-CN.md)。其实施不得延后生产
auto-migration/readiness、认证 engine profile、可观测性、容量规划和审计能力。
现状与目标所有权边界记录在 [Control 模块地图](control-module-map.zh-CN.md)。

## 背景

v0.3 的 domain 拆分已经让 `app.py` 成为小型组合根，并为 jobs、workers、
manifests、model profiles、remote administration 和 diagnostics 建立独立 router。
剩余复杂度集中在 domain core 及其隐式跨域调用：

- jobs、manifests 和 workers 共享调度状态，并通过 lazy wrapper 相互调用；
- leaf helper、query 和 HTTP adapter 中都存在事务边界；
- scan、claim、lease、attempt、retry、fence 和 recovery 没有唯一内部所有者；
- `ocr_platform.control.service` 兼容 façade 暴露很大的、依赖 monkeypatch 的运行时
  surface；
- 当前 OpenAPI golden 只锁定 path 和 operation 数量，没有锁定完整 schema、status
  和 error contract。

支持这些判断的审计快照记录在模块地图中。文件大小只作为评审证据，不是重构目标或
验收指标。

## 证据摘要

当前快照包含：

- 138 行的组合根；
- 1,678 行 jobs core、2,100 行 manifests core、1,059 行 workers core；
- 37 个 lazy 跨域 wrapper；
- 四个可变 core 中共 50 个显式事务点；
- 兼容 façade 运行时导出 286 个名称，8 个测试/工具文件引用，21 个 monkeypatch；
- 13 个集中 ORM class；
- OpenAPI golden 覆盖 47 个 path 和 49 个 operation，但未锁定完整 schema。

这些数字只是当前时间点的审计。目标是明确所有权和可测试行为，不是减少行数。

## 目标

- 只允许 application use case 协调多个 domain。
- 每个业务操作只有一个 commit 或 rollback 边界。
- 集中调度状态转换和恢复策略。
- 消除 domain private core 之间的 import 和全部循环依赖。
- 保持所有 HTTP、数据库、migration、状态机和输出契约。
- 通过显式迁移删除历史 Control service façade。
- 让并发、迟到 replay 和 attempt fencing 可在 PostgreSQL 上独立测试。

## 非目标

- 重写 OCR、layout、table、Markdown、manifest 或调度算法。
- 将 Control 拆成微服务。
- 按 domain 拆散 ORM model。
- 引入通用 Repository 或 Unit of Work 框架。
- 采用 Alembic、异步数据库访问或替换 ORM。
- 更改 Control UI 框架。`ui/main.js` 的体积继续记为技术债，但 UI 重构不在本
  RFC 内。
- 修改 HTTP path、状态值、CLI 行为、manifest JSONL 或产物格式。本 RFC 的结构 PR
  不修改数据库 schema 或 0001-0019 migration；独立评审的 additive 运维 migration
  继续由上层 RFC 管理。

## 决策

### Application use case 拥有协调权

HTTP router 只转换请求和响应；application use case 执行业务操作。一个 use case
可以调用多个 domain policy 或 query port，但 domain private core 不得相互 import。

跨域结果通过显式值返回，不再依赖动态 façade attribute 或 monkeypatch 转发。

### 每个业务操作一个事务

command 或 application use case 拥有一个数据库事务：

- 成功时 commit 一次；
- 失败时 rollback 一次；
- leaf mutation 只有在需要取得 ID 或检查约束时才能 `flush()`，不得 commit 或
  rollback；
- query 默认只读，不得 commit 或 rollback；
- 通过静态检查和集成测试约束事务策略。

query module 不得执行 DML。HTTP `GET` 和 `HEAD` handler 不得把 DML 隐藏在 query
内部。兼容迁移期间，现有 job-summary route 可以显式调用有名称的
`refresh_job_summary` application use case，再执行只读查询。该 route 是 allowlist
中唯一允许写入的 `GET`；application 层调用必须保持可见并可独立测试。如果后续从
route 中移除 refresh，allowlist 变为空。

当前 model-profile `GET` list path 可能创建默认 profile。默认创建迁移到显式
startup/bootstrap 初始化步骤，随后 list query 改为纯只读。

### Scheduling 成为无 HTTP 的内部 domain

新增没有 router 的内部 `scheduling` domain，负责：

- `ScanUnit`、`WorkShard` 和 `ShardAttempt` 状态转换策略；
- scan unit 和 shard 的 claim 选择；
- lease 创建、续租、过期和 reclaim；
- attempt 编号与 active-attempt 校验；
- retry 资格和 retry status；
- restart fencing 和迟到 replay 拒绝；
- 所属调度实体的 stop/recovery 协调。

scheduling 不拥有 Job lifecycle、manifest 构建与完整性，也不拥有 Server identity
和 heartbeat。application use case 负责协调这些所有者和 scheduling。

### 实体所有权

| 所有者 | 主要实体和策略 |
| --- | --- |
| jobs | `Job`、`JobCounter`、`JobEvent`、`JobLog`、`JobFile`；lifecycle、stop/archive/delete、counter、event 和 log policy |
| manifests | `Manifest`；snapshot 构建、freeze、integrity、path validation |
| workers | `Server`；registration、heartbeat、status、capability 和 path eligibility |
| model profiles | `ModelProfile`；profile validation、provenance、effective model configuration |
| scheduling | `ScanUnit`、`WorkShard`、`ShardAttempt`；claim、lease、attempt、retry、fence、recovery |
| diagnostics | 只读 health、migration、source 和 deployment diagnostics |
| remote admin | 显式启用的 remote-worker operation |

ORM 声明继续集中在 `ocr_platform.control.models`。所有权指 policy 和 mutation
authority，而不是 model 的物理位置。

### 状态转换策略显式集中

每种有状态实体只有一个 transition policy module。调用方请求 transition，不直接
写 status 字符串。policy 定义：

- 允许的 source 和 target state；
- terminal replay 幂等；
- attempt 和 server fencing；
- timestamp、lease 清理和 terminal side effect；
- 受控 failure 和 retry category。

静态检查禁止在所属 policy 以外修改 status，只允许有明确文档的 bootstrap 或
migration 例外。

### 最后删除兼容 façade

在内部 import、测试、工具和文档集成完成迁移之前，保留
`ocr_platform.control.service`。其他 Control 改动稳定后，再用独立 PR 删除。

| 旧导入功能簇 | 新所有者 |
| --- | --- |
| job 创建、lifecycle、event、log、counter | `ocr_platform.control.domains.jobs` application/command/query surface |
| manifest 创建、freeze、integrity | `ocr_platform.control.domains.manifests` surface |
| server registration、heartbeat、eligibility | `ocr_platform.control.domains.workers` surface |
| scan/shard claim、lease、attempt、recovery | 显式 Control application/command surface；内部 scheduling domain 不属于兼容 API |
| model profile 配置 | `ocr_platform.control.domains.model_profiles` surface |
| deployment 和 source 检查 | `ocr_platform.control.domains.diagnostics` surface |
| remote worker operation | `ocr_platform.control.domains.remote_admin` surface |

删除 façade 之前会生成并检查精确的 symbol-level mapping。v0.4 release notes 会明确
这项 Python import 兼容变化。

## 依赖规则

目标依赖方向：

1. 集中 ORM 和中立 Control contract；
2. domain policy 和只读 query；
3. application use case；
4. HTTP router、CLI adapter、diagnostics adapter 和组合根。

domain private `core` 可以依赖 model 和本域 contract，但不能依赖其他 domain private
core。application use case 是唯一跨域协调者。scheduling 仅为内部域，不依赖 HTTP。

## 兼容契约

重构保持：

- canonical OpenAPI path、operation、request/response schema、status 和 error body；
- migration 0001-0019 及其 checksum，以及所有现有数据库 table、column、constraint
  和状态值。新增 additive 运维 migration 需要独立评审，不得混入结构 PR；
- Job/Shard/attempt 行为，包括 terminal monotonicity 和 replay idempotence；
- CLI flag 和 exit code；
- manifest JSONL 和输出格式；
- 鉴权、remote-admin 默认值、AGPL source offer 和 package data。

唯一有意的公开 Python 变化是在 v0.4 末尾删除
`ocr_platform.control.service`。domain private module 仍不属于公开接口。

## PR 顺序

### PR 0：文档

合入本 RFC 和模块地图，不修改运行时行为。

### PR 1：完整 contract 和静态门禁

- 记录 canonical OpenAPI schema、status 和 error body，不再只检查数量。
- 锁定 migration 0001-0019、数据库 metadata 和状态值。
- 新增 private 跨域 import、循环依赖、事务、read DML 和状态修改边界检查。
- 生成 service façade symbol inventory 和迁移表。

### PR 2：Auto-migration 和 readiness

先落实运维 RFC 的生产 migration 默认值和可操作 readiness，再进行结构调整。

### PR 3：认证 engine profile

绑定认证 provenance 和可审计风险接受，不与 Control 拆分混在同一 PR。如果
provenance 需要 additive migration 0020，将其作为运维数据库变化独立评审，并遵守
下文回滚门禁。

### PR 4：可观测性、容量和审计

交付固定 label 告警、只读容量输出和持久审计证据，优先级高于内部拆分。

### PR 5：Settings、bootstrap 和低风险事务迁移

- 将 settings 和 database/session 构造改为显式依赖。
- 把低风险 leaf 的事务所有权迁移到 application use case。
- 使用 characterization test 保持 API 和 SQL 行为。

### PR 6：Scheduling kernel

- 建立无 HTTP 的 scheduling domain。
- 迁移 scan/shard claim、lease、attempt、retry、fencing 和 recovery policy。
- 验证 PostgreSQL contention、recovery、late replay 和 attempt fencing。

### PR 7a：拆分 jobs core

将 job lifecycle、projection、event、log 和 counter 迁移到 jobs owner。该 PR 可
独立回滚。

### PR 7b：拆分 manifests core

将 manifest construction、path、freeze 和 integrity 迁移到 manifests owner。该 PR
可独立回滚。

### PR 7c：拆分 workers core

将 worker identity、heartbeat、eligibility 和 preflight 迁移到 workers owner，
消除剩余 private core 跨域 import。该 PR 可独立回滚。

PR 7a-7c 集中剩余 transition policy，不混入 migration、API 或算法变化。

### PR 8：删除 façade

迁移仓库内全部测试和工具，校验 symbol migration table，再删除
`ocr_platform.control.service`，并提供 release-note 指引。

### PR 9：RC 和发布

v0.4 发布前执行完整兼容、package、PostgreSQL、恢复、安全和 source-offer 门禁。

每个结构 PR 必须可独立合并，且不能同时包含数据库 migration、HTTP contract 或
算法变化。

## 进入门禁

文档和设计评审可以立即进行。观察期从 v0.3.1 Release 发布时间
2026-07-26 14:51:07+08:00 起算。运行时代码实施不早于
2026-08-02 14:51:07+08:00，且必须满足：

- v0.3.1 长时间、多周期验证已经按记录的风险接受完成评审；
- 至少七天观察期内没有新的 P0/P1；
- 没有未解决的数据丢失、重复 claim/artifact、migration、replay、shutdown、鉴权、
  source-offer 或资源泄漏问题；
- 完整 contract baseline 和 rollback procedure 已批准。

## 退出门禁

- 完整 canonical OpenAPI、status 和 error contract 不变。
- migration 0001-0019 及其 checksum 和现有数据库状态值不变。任何新增运维
  migration 都必须 additive、独立门禁，并且不出现在结构 PR 中。
- private core 跨域 import 为 0。
- 循环依赖为 0。
- leaf mutation 和 query 中 commit/rollback 为 0。
- query module 中 DML 为 0。
- HTTP `GET` 和 `HEAD` handler 中隐藏 DML 为 0。兼容迁移期间，显式 DML allowlist
  只包含具名的 job-summary `refresh_job_summary` application 调用；如果移除该
  refresh，allowlist 变为空。
- 有状态实体的 transition 集中到所属 policy。
- `ocr_platform.control.service` 引用和 monkeypatch 为 0。
- 真实 PostgreSQL concurrency、recovery、late replay、lease 和 attempt-fencing
  测试通过。
- 全量 CI、Python 和安装矩阵、wheel/package data 与 AGPL source-offer 检查通过。

行数不是退出门禁。

## 回滚

本 RFC 的结构 PR 不包含 migration，并保留 commit 级 revert 路径。façade 存在时，
结构 PR 可以在保持 import 和 HTTP contract 的前提下回退到旧实现。若发现遗漏的
集成依赖，façade 删除 PR 作为整体回滚。

运维 PR 3 可能为认证 profile provenance 新增 migration 0020。应用任何 0019 之后
的 migration 前，发布方案必须选择向后兼容的维护路径，或准备并验证升级前数据库
snapshot 和回滚流程。schema 已超前的数据库不得直接运行 v0.3.1。应用此类
migration 后，必须按已选择的兼容或 snapshot 流程回滚，并校验 migration checksum
和 worker compatibility，不能无条件部署旧 wheel。

## 风险

- 即使 SQL 语句等价，移动事务边界也会改变锁持有时间和 deadlock 行为。
- scheduling 迁移不完整会造成 transition 双重所有权，并重新引入 late replay 或
  reclaim 缺陷。
- job-summary 显式 refresh 例外可能再次被隐藏到 query 中。
- 删除 façade 可能破坏仓库外不可见的 Python import。
- 如果静态规则例外过宽，只会增加间接层而不能改善所有权。

缓解措施是小型 PR、contract golden、真实 PostgreSQL contention 测试、symbol
inventory 和可回滚 adapter。

## 待决问题

- 显式 summary refresh 应继续使用现有 route，还是作为可观测 application action，
  同时保持 wire behavior？
- migration 和 bootstrap 是否需要状态转换例外？
- 删除 service façade 前，对外部使用方提供何种 deprecation notice 才充分？
- 只读 capacity query 应共享 diagnostics 基础设施，还是独立 application query？
- 哪些锁持有时间和 deadlock 阈值应阻塞结构 PR？
