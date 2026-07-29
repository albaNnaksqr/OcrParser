# Control 契约基线

[English](control-contract-baselines.md) | 中文

v0.4 Control 重构使用经过审查并提交到仓库的 fixture，区分内部移动和外部行为
变化。当前基线记录：

- 完整 canonical OpenAPI 文档；
- 通过真实 `TestClient` 请求采集的代表性 HTTP 成功与错误行为，包括鉴权、
  Remote Admin、readiness、validation、not-found、bad-request 和 conflict。
  每个场景保存完整 JSON 响应体；只有明确记录动态字段路径、替换值和原因后才允许
  normalization，且目标与替换值都必须是 scalar；validation error 数组保留原始
  顺序和全部错误项；
- 机器生成的 49 项 canonical OpenAPI operation matrix。每项记录 OpenAPI 声明
  状态、AST 发现的 router 与显式调用 service 状态分支、行为场景证据，以及每个
  未执行分支独立的源码证据与原因。行为证据必须绑定精确源码分支，只有全局 API
  token middleware 和 FastAPI request validation handler 可以共享。独立 gate
  扫描全部 `ocr_platform/control/**/*.py` 运行时代码并与 matrix transport
  evidence 对齐；domain `core.py`、`commands.py`、`queries.py` 禁止引入 FastAPI
  或 Starlette transport；
- 通过真实 service 调用采集 WorkShard 与 ScanUnit 的 scheduling 行为，包括
  claim ordering、lease renew/expire/reclaim、attempt fencing、成功/失败终态
  replay、混合状态 stop 和 recovery finalization。PostgreSQL 并发
  `SKIP LOCKED` 明确保留为外部必跑门禁；SQLite 场景与 SQL 编译不会被表述为
  并发证明；
- ORM 表、列、类型、可空性、默认值、主键、外键、索引和 check constraint；
- SQLite 实际创建的索引，包括 `PRAGMA index_list` 返回的自动主键索引；
- 13 个状态 surface。封闭集合来自数据库 check、Pydantic Literal、domain 常量
  或 AST 发现的 transition；每个封闭值都有源码证据，并区分真实行为观察和仅源码
  覆盖。ShardAttempt 与 manifest freeze projection 绑定 AST 验证的源码关系，
  不依赖固定行号。外部或事件输入字符串保持开放；特别是 worker 可提供的
  `ManifestIntegrityResponse.status` 不会被误写成 Control 自有的穷举 enum。
- site 级 architecture debt，包括跨 domain core import 与图 SCC、transaction
  调用、query 中的直接/语义 mutation，以及 owner policy 外状态写入。stable ID
  使用 `module:function:symbol-or-operation:ordinal`；行号只作证据，每个 site
  另存 normalized AST fingerprint。query 门禁识别 ORM 属性写入、SQLAlchemy
  bulk API，以及通过 `execute`、`scalar`、`scalars` 执行的 DML；function-aware
  语义 call graph 会跟踪 module-level 与函数内 lazy import，并覆盖同一 domain
  的全部 runtime module，包括后续的 command 和 application 模块；
- 已删除 `ocr_platform.control.service` façade 的 tombstone 门禁。旧模块必须
  不存在，direct、relative、dynamic、embedded import 和字符串 monkeypatch
  均被禁止。仓库测试与工具曾消费的 24 个 symbol 已在
  `tests/fixtures/contracts/control_facade_inventory.json` 和
  [Control façade 迁移表](control-facade-migration.zh-CN.md) 中记录完成目标。

迁移历史不会复制到第二份 fixture。现有
`tests/fixtures/contracts/control_migration_checksums.json` 继续作为 migration
版本和固定字节 SHA-256 的唯一真相源。数据库 metadata fixture 只保存对它的路径
和摘要引用。

## 审查流程

测试只对比生成结果和已提交 fixture，绝不在测试中重写文件。维护者必须显式执行：

```bash
python3 tools/control_contracts.py refresh
python3 tools/control_contracts.py check
python3 tools/control_architecture_debt.py refresh
python3 tools/control_architecture_debt.py check
python3 tools/control_facade_inventory.py refresh
python3 tools/control_facade_inventory.py check
python3 -m pytest -q tests/test_control_contracts.py tests/test_control_scheduling_contracts.py tests/test_control_architecture_debt.py tests/test_control_facade_inventory.py tests/test_v01_behavior_contract.py tests/test_migration_runner.py
```

连续执行两次 `refresh`，第二次执行后工作区必须保持字节级不变。需要完整审查
fixture diff。意外出现的 OpenAPI、schema、状态或 migration reference 变化会阻塞
结构重构；不能通过盲目刷新 fixture 接受变化。

现有 v0.1 route-path golden 继续作为较小的独立门禁。architecture-debt gate
采用递减规则：当前 `(stable ID, AST fingerprint)` 必须是审查基线的子集。删除
site 允许通过；新增 site、替换 AST、新增 domain edge 或新增 SCC 都会失败。
façade tombstone gate 要求模块和全部仓库引用保持为零。
