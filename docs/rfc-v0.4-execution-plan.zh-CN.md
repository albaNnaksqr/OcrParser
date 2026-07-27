# OcrParser v0.4.0 执行计划

[English](rfc-v0.4-execution-plan.md) | 中文

状态：已批准并进入实施。Maintainer 已于 2026-07-27 明确取消原
`2026-08-02` 日历门禁，并授权立即实施。该 override 只取消日期等待；P0/P1、
单写者、兼容性、评审和验收门禁仍然必须执行。

本文将 [v0.4 运维成熟度 RFC](rfc-v0.4.zh-CN.md) 和
[Control 内部重构 RFC](rfc-v0.4-control-internals.zh-CN.md) 转化为有序交付计划。
当前审计快照和模块边界见 [Control 模块地图](control-module-map.zh-CN.md)，已提交
fixture 的审查流程见 [Control 契约基线](control-contract-baselines.zh-CN.md)。

## 目标形态

v0.4.0 保持 Control 为模块化单体，并将其建设为权威控制平面：

- Application use case 负责跨域协调和事务；
- Domain policy 负责状态转换；
- 无 HTTP 的内部 `scheduling` 域负责 claim、lease、attempt、retry、fence 和
  recovery；
- 显式运维、认证 engine profile、有界可观测性、容量建议和审计证据成为生产能力；
- 现有 HTTP、数据库、CLI、manifest、输出和状态值契约保持兼容。

唯一有意的公开 breaking change，是在 v0.4 末尾删除
`ocr_platform.control.service` Python 兼容 façade。

## 执行治理

公开仓库 `main` 是唯一源码主线。全部工作使用同一 checkout，并执行严格单写者规则。

| 角色 | 职责 |
| --- | --- |
| 主 Agent | 只负责调度、diff 审查、证据核验、验收和发布授权；不编辑、stage、commit、push、tag 或发布 |
| `migration_bridge_agent` | v0.3.2 migration bridge 和 PostgreSQL migration-first 初始化 |
| `contract_guardian` | 完整兼容 golden、依赖门禁和 façade 清单 |
| `operations_builder` | migration/readiness 策略、认证、可观测性、容量和审计 |
| `control_refactorer` | 显式事务、scheduling kernel、domain 所有权拆分和 façade 迁移 |
| `validation_operator` | 隔离 PostgreSQL、恢复、package、soak 和引擎集成验证；不写仓库 |
| `release_integrator` | 获得授权后负责版本、双语发布文档、clean wheel、tag 和 GitHub Release |

任何时刻只允许一个 Agent 修改 tracked files。验证只能在隔离任务目录中并行，且不得
改变仓库状态。每个代码 wave 都必须经过主 Agent 审查、定向测试、完整 CI 和干净工作区
确认，才能切换下一位写入者。

如果验证或发布观察发现新的 P0/P1，v0.4 冻结，问题返回 v0.3.x 维护版本处理。

## 交付顺序

### Wave 0：v0.3.2 schema bridge

在 v0.4 运行时开发之前发布 v0.3.2。

- 新增 additive migration `0020_model_profile_certification.sql` 及集中 ORM 映射。
- 为 `ModelProfile` 定义可选的一对一 `model_profile_certifications` 记录，字段包括：
  `profile_id`；`enforcement`（`off`、`verified` 或 `certified`）；
  `status`（`contract_only`、`verified`、`certified` 或 `blocked`）；
  parser、model、runtime 及可选 layout revision/digest；fixture-set 和 evidence
  digest；`certified_at`；`risk_acceptance_json`；`updated_at`。v0.3.2 不自动为
  现有 Profile 插入记录。
- 认证记录不得保存 key、凭据、内网地址、私有路径、客户文档或 OCR 正文。
- v0.3.2 不通过 HTTP 暴露认证，也不启用认证门禁；缺少认证记录不产生行为变化。
- PostgreSQL 初始化必须改为 migration-first。当前
  `create_all()` 先于 migration 的路径是发布阻塞项，因为新数据库可能先取得最新 ORM
  schema，再由 migration catalog 补记历史。PostgreSQL 必须以 checksum-verified
  migration catalog 为 schema 权威；SQLite 直接开发模式可以保留 create-all 便利路径。
- 验证全新 PostgreSQL、v0.3.1 到 v0.3.2 升级、并发 advisory lock、checksum，以及
  v0.3.2 对 schema 0020 的无行为兼容。

应用 0020 后，v0.3.2 是回滚下限。识别 0020 的数据库经兼容验证后可以回滚到 v0.3.2，
但不得直接由 v0.3.1 提供服务。不支持破坏性降级；如需降到 v0.3.2 以下，必须恢复经过
验证的升级前快照。整个 v0.4.0 不新增 migration 0021，也不修改 0020 checksum。如确需
新的 schema 变化，必须先单独发布兼容维护桥接版本，并重新评审回滚下限，之后才能恢复
v0.4 工作。

### PR 1：完整契约与递减式静态门禁

- 固化 canonical OpenAPI JSON、请求/响应 schema、status code、error body、
  database metadata、migration checksum、状态值和 claim/attempt 行为。
- 在包含 migration 0020 的准确 v0.3.2 commit 上重新捕获 PR 1 fixture。下面的
  Wave 0 前审计仅为证据，不是最终门禁。
- 按准确 site 和 symbol 保存违规清单。后续 PR 可以删除条目，但不得在保持计数不增长的
  同时，用新违规位置替换旧位置。
- 为 `ocr_platform.control.service` 生成并测试 symbol-level 迁移表。
- 完整 HTTP behavior matrix 必须覆盖预期的 400、401、403、404、409、422 和 503。

commit `6b7cff3` 的 Wave 0 前审计记录如下：

| Surface | 审计值 |
| --- | --- |
| OpenAPI | 47 paths、49 operations、56 schemas；canonical SHA-256 `2217e4551be81570540c406d501a2c1d23aba15fca31f9d933c6434abc0b76ad` |
| 数据库 | 11 tables、171 columns、15 ORM indexes、11 foreign keys；migrations 0001–0019 |
| 跨 domain core 依赖 | 42 sites / 44 symbols，其中 17 个 private dependencies、37 个 lazy wrappers |
| 显式事务调用 | 共 50 个：29 commit、7 rollback、14 flush |
| 语义上的读侧写入 | 4 个 allowlisted sites：3 个 Job-summary queries 和 Profile list |
| Status-like writes | 40 sites |
| 兼容 façade | 286 exports；7 个真实 consumer files；19 个 AST import sites 加 1 个 embedded import；21 个 monkeypatch sites；23 个 unique consumed symbols |

这些值不得直接复制为最终 PR 1 fixture。PR 1 必须在 v0.3.2/0020 落地后重新生成完整
基线，再用按 site 子集递减的清单推进到零。除此之外，此 PR 只做行为刻画，不修改运行时
行为。

### PR 2：显式 migration、readiness 与 bootstrap

- 新增不可变 `ControlSettings`，显式构造 session factory、remote executor、limits、
  authentication 和 runtime mode。
- 新增 `OCR_PLATFORM_AUTO_MIGRATE`。PostgreSQL 未设置时默认关闭，仅显式设置为 `1`
  才允许升级；SQLite 直接开发保留有文档说明的便利路径。
- `/healthz` 继续表示进程健康。PostgreSQL schema 落后或 checksum 不一致时，
  `/readyz` 返回 `503`，并提供可执行的
  `ocr-platform-migrate plan|apply|verify` 指引。
- 未 ready 时，业务 API 返回稳定 `503`；保留 diagnostics、database、source offer
  和 legal 接口用于恢复。
- `OCR_PLATFORM_REQUIRE_CURRENT_MIGRATIONS` 保留一个版本，作为严格启动失败兼容开关。
- 将默认 ModelProfile 创建移到显式 bootstrap，使 Profile list query 变为纯读。

### PR 3：认证 Engine Profile

- 在 ModelProfile 请求和响应契约中增加可选 `certification` 对象，现有字段不变。
- 没有认证记录的旧 Profile 等价于 `status=contract_only`、
  `enforcement=off`，继续允许运行。
- Certified 必须具有 parser/model revision、不可变 runtime digest、fixture-set
  digest、evidence digest，以及适用时的 layout provenance。
- 启用门禁的 Verified 必须具有具名、带时间且说明原因的风险接受记录。
- Agent 新增可选 engine provenance 文件配置。heartbeat 只上报 revision/digest，
  不上报地址或凭据。
- Job preflight 和创建共用一套认证策略：`off` 只展示；`verified` 接受 Verified 或
  Certified；`certified` 要求 Profile、Agent 和 build 精确匹配。
- 返回固定错误：
  `model_profile_certification_missing`、
  `model_profile_certification_mismatch` 和
  `model_profile_risk_acceptance_required`。

不自动将任何已有引擎升级为 Certified。

### PR 4：可观测性、容量与审计

- 新增受 token 保护的 `/api/system/metrics`，使用 Prometheus 文本格式。
- label 仅允许枚举的 engine、stage、status 和 failure/fallback category；禁止把
  Job ID、路径、自由错误文本、凭据或文档内容用作 label。
- 为 diagnostics 向后兼容地增加可选 `capacity`、`audit` 和 `alerts` 对象。
- 容量输出 ready/available worker slots、pending shard depth、最近 60 分钟 observed
  pages/hour、estimated drain time、`none|low|medium` confidence 和固定
  recommendation code。观测不足 10 页时不生成 ETA。
- 提供由运维方启用的 migration drift、heartbeat age、stale lease、spool backlog、
  stage failure、fallback 和 artifact audit 告警模板。
- 复用 JobEvent、ShardAttempt、manifest integrity 和 output audit 证据，不记录 OCR
  内容。容量功能只给建议，不自动扩缩 Worker 或模型服务。

### PR 5：事务与依赖基础

- 完成显式依赖注入，删除模块级可变 registry。
- 每个 application command 只拥有一个 `session.begin()`：成功 commit 一次，失败
  rollback 一次；leaf mutation 可以 flush，但不得 commit/rollback；query 不执行 DML。
- 先迁移低风险操作：Profile CRUD、Job event/log/counter 和 Manifest registration。
- 保持 Job summary wire behavior：显式调用 `refresh_job_summary` application 后再执行
  纯读 query；不得将 mutation 隐藏在 query 或 HTTP adapter 中。

### PR 6：Scheduling Kernel

- 新增无 router、无公开兼容 API 的内部 `scheduling` 域。
- 由它唯一负责 ScanUnit、WorkShard、ShardAttempt 的 transition、claim ordering、
  lease renew/expire/reclaim、attempt numbering、retry、server/attempt fencing、
  terminal replay、stop 和 recovery。
- Worker registration/heartbeat、Job stop 和 shard update 由 application use case
  协调。
- 保持现有 `FOR UPDATE/SKIP LOCKED`、claim order、lease window、attempt
  uniqueness 和 terminal-state monotonicity。

此 PR 改变所有权，不改变 HTTP 契约、数据库 schema 或调度算法。

### PR 7a–7c：按所有权拆分 Domain

- **PR 7a — jobs：** lifecycle、projection、event、log 和 counter。
- **PR 7b — manifests：** construction、path policy、freeze 和 integrity。
- **PR 7c — workers：** identity、heartbeat、eligibility 和 preflight。

每个 PR 独立可回滚，不包含 migration、HTTP 或算法变化。PR 7c 完成后，private core
跨域 import、依赖环、query DML、leaf commit/rollback 和 owner policy 外状态赋值全部
归零。

### PR 8：删除 Façade

- 按 symbol-level 映射迁移仓库内全部生产 import、test、tool 和文档化集成。
- 删除 wildcard export、动态转发和 `ocr_platform.control.service`。
- HTTP 继续作为稳定公开接口；显式 domain command/query/schema 是受支持的 Python
  集成面；private core、policy 和 scheduling 路径不是兼容 API。

这是 v0.4.0 唯一有意的公开 breaking change。

### PR 9：Release Candidate 与正式发布

- 发布 `0.4.0rc1`，提供双语升级说明、façade 迁移表、架构文档和告警示例。
- 执行完整自动化门禁、隔离的多周期恢复验证和 4 小时 mock soak；用支持的真实引擎
  快速复验 canonical fixtures，但不与历史吞吐比较。
- 对准确 RC 观察 48 小时且无新 P0/P1。验收后，`0.4.0` 只允许版本和最终 Release
  文档发生 tracked-file 变化。
- 从与 tag 一致的 clean commit 构建，确认 `release_build=true`、`dirty=false`，
  再发布已验收 wheel 和 GitHub Release。
- 正式发布后观察 7 天；P0/P1 将暂停后续工作并启动范围明确的维护版本。

公开报告只记录脱敏结论和产物 digest，不公开私有基础设施拓扑、endpoint、凭据、路径
或客户数据。

## 阶段验收与回滚

每个 PR 必须可以独立合并，并使全部行为刻画门禁保持在已验收 site inventory 的子集
内。结构 PR 不包含 schema、HTTP 或算法变化。结构 PR 验收失败时，在 façade 仍维持
原契约的前提下，以完整 commit 为单位回滚。

Wave 0 和 PR 2–4 属于运维变化，必须各自提供明确回滚记录。应用 0020 后：

- v0.3.2 是允许操作该数据库的最旧 wheel；
- v0.4 runtime 回滚只能在 migration、Worker 和认证兼容检查通过后使用 v0.3.2；
- 回滚到 v0.3.2 以下必须恢复经过验证的 0020 之前快照；
- checksum 不一致或出现未知 migration 时，启动和回滚都必须阻塞。

Migration 0020 是 v0.4.0 的 schema 上限。禁止新增 0021 或修改 0020 checksum；新的
schema 需求必须先回到单独发布的兼容桥接版本，并重新评审回滚线。

RC 完成验证后如有任何 tracked-file 变化，该候选立即失效，必须重新构建并重复受影响
的门禁。

## 兼容与验收

以下保持不变：console script、CLI 参数和退出码、已有 HTTP path/field/status、
Job/Shard/Attempt 状态值、manifest JSONL、Markdown/JSON/sidecar、输出目录、
migration 0001–0019 及 checksum、Parser 顶层 façade，以及至少保留到 v0.5 的旧
fallback status。

新增接口均为 additive：migration 0020、可选 Profile certification、
`OCR_PLATFORM_AUTO_MIGRATE`、可选 Agent provenance 配置、
`/api/system/metrics`，以及 diagnostics 的可选 capacity/audit/alerts 字段。
唯一 breaking interface 是删除 `ocr_platform.control.service`。

发布门禁包括：

- 当前全部测试及新增行为刻画和 policy 测试，覆盖 Python 3.10–3.12；
- base/platform/s3/layout/full clean-wheel 安装和全部 console scripts；
- PostgreSQL fresh install、0.3.1→0.3.2→0.4.0、migration lock/checksum，以及
  v0.4.0→v0.3.2 兼容回滚；
- 并发 claim、lease、reclaim、late replay、attempt fencing、Control 中断、spool
  replay 和 Agent restart；
- certification missing/mismatch/risk acceptance/legacy Profile；
- bounded label、secret redaction、authentication、Remote Admin、AGPL `/source`、
  UI/package data 和 wheel provenance；
- claim stress、Job summary 和 mock E2E 性能回退不超过 10%。

数据丢失、重复 claim/artifact、错误 migration、任务永久卡住、replay 失败、
shutdown 后继续上报、鉴权/source-offer 缺陷或资源持续泄漏，均阻塞发布。

## 固定约束

- 本计划中的版本是 v0.4.0，不是 v4.0.0。
- 公开仓库是唯一源码主线，不创建独立实现 checkout。
- Control 保持一个可部署的模块化单体。
- 不引入微服务、Alembic、异步数据库、通用 Repository 框架或新前端框架。
- 不修改 OCR、layout、table、Markdown 或调度算法。
- 模型质量限制可以明确保持 Verified；集成、一致性、migration 和恢复缺陷必须阻塞
  发布。
