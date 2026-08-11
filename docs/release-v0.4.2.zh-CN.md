# v0.4.2 发布说明

[English](release-v0.4.2.md) | 中文

`0.4.2` 面向执行大规模 OCR 任务的操作人员优化 Control UI。变更仅限浏览器工作台；
Parser、Agent、Control HTTP、数据库、migration、调度、manifest 和输出契约均保持不变。

## 工作台

- `#jobs` 是默认任务队列与任务排障视图。
- `#jobs/new` 提供全宽四步任务流程。草稿只保存在内存中，仍以后端 preflight 为
  最终依据；请求级 API key 不会在 Review 中显示，也不会由浏览器持久化。
- `#workers` 集中展示 readiness、共享路径资格、容量、资源、版本和 spool 证据；
  注册、扩容和 Remote Admin 保留在 Advanced 区域。
- `#system` 集中展示 Deployment Doctor、数据库与 migration 证据、Model Profiles、
  capacity、audit、alerts、源码和许可证入口。

## Deployment Doctor

Diagnostics 始终只读。UI 会解释已有问题、对任务的影响和建议操作。Migration drift
展示明确的 `status → plan → apply → verify` 流程；UI 不执行 migration、不修改配置、
不启动服务，也不调用 sudo。

## 验证

本版本继续保留 Python 3.10-3.12、安装 profile、PostgreSQL、mock E2E、部署 Bundle
和文档门禁。独立的 Python 3.12 Playwright Chromium job 从构建 wheel 安装 Control，
验证 UI package data、鉴权、导航、Doctor、Worker 详情、任务 preflight 与创建、桌面
布局、焦点、label 和 live region。
