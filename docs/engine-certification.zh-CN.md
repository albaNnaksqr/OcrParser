# 引擎认证

[English](engine-certification.md) | 中文

本矩阵将 parser 发布就绪与可选的真实模型引擎认证分开。GitHub Release 不需要
启动模型服务；引擎认证是实际启用某个引擎时带日期的独立部署记录。

## 状态定义

- **Certified（已认证）**：准确的 parser commit 和模型 revision 已通过 endpoint
  健康、公开 fixture 端到端解析、输出抽查和部署许可证审查。
- **Verified（已验证）**：真实服务通过功能检查，但大范围质量、可观测性、性能或
  许可证审批等至少一项生产门禁仍未关闭。
- **Contract only（仅契约）**：mock/单元契约通过，但没有当前真实服务证据。
- **Blocked（阻塞）**：必需服务或模型无法启动，或未通过最小功能检查。

真实模型 smoke 通过不代表所有文档类型的质量都已批准。模型副本数、服务版本、
GPU 预算或模型 revision 不同的结果，不能直接判定为性能回退。

## 当前矩阵

证据 commit：`9c3bea6`（`v0.2.0`）；矩阵刷新日期：2026-07-17。
机器可读的来源记录维护在
[`engine-certification-records.json`](engine-certification-records.json)。条件通过
记录有意保留空的 runtime digest；只有 v0.3 rc 复验写入锁定镜像 digest 和准确
parser commit 后，状态才能升级为 **Certified**。

| 引擎 | 服务拓扑 | 契约 | 真实服务与输出 | 许可证审查 | 状态 |
| --- | --- | --- | --- | --- | --- |
| DotsOCR（`dotsocr`） | 一个 OpenAI-compatible VLM endpoint | 通过 | 本次 Spark 刷新未重跑，仅引用此前发布验证 | Parser 代码 MIT；AGPL 源码入口已实现；模型批准仍取决于部署 | **Verified，未刷新** |
| MinerU（`mineru`） | 同一个 OpenAI-compatible VLM endpoint 执行 layout 和 recognition | 通过 | 使用已验证的 vLLM backend 时两个公开 fixture 都产生可读输出；SGLang 返回语义无效的重复 token | 模型和已验证 runtime 为 Apache-2.0；AGPL 源码入口已实现 | **Verified，仅 vLLM 且条件通过** |
| PaddleOCR-VL（`paddleocr-vl`） | PP-DocLayoutV2 `/detect` 加 OpenAI-compatible VLM | 通过 | 两个公开单页 fixture 都完成；正文可读，但窄票据表格出现空单元格，page status 不能区分正常两阶段输出与真实 fallback | Paddle 模型与导入 layout 源码均为 Apache-2.0；AGPL 源码入口已实现 | **Verified，条件通过** |

必需依赖 PyMuPDF 采用 AGPL/商业许可双许可证。本仓库已经实现 AGPL 源码提供路径；
每次部署仍必须确认 `/source` 指向准确运行源码，详见
[AGPL 合规](agpl-compliance.zh-CN.md)。

本次刷新使用的公开 fixture：

- `simple_text_1p.pdf` SHA256
  `eb542ecf8b1b4052d32b3f69449d3e875f8a9f8074851ec6b964f32ca3c259ff`；
- `receipt_narrow_1p.pdf` SHA256
  `a2ef9fd25513654491136d69b6018ce6032a699f0ebfceaf06769389a63e6bb5`。

## MinerU 证据

- Parser：`v0.2.0`，commit `9c3bea6`。
- 模型：[OpenDataLab/MinerU2.5-Pro-2604-1.2B](https://huggingface.co/opendatalab/MinerU2.5-Pro-2604-1.2B)，
  Hugging Face revision `d3f5e08d073c21466bbabe21c71bb1e9c2e595da`，
  ModelScope revision `5ce0a587eda752aa7e4c45e2198ec4c2f00e0bd8`，
  Apache-2.0；`model.safetensors` SHA256
  `f2650d91aaa619534980445034f62cde27fc3fa0430aaf5c3302b91179cad0c5`
  与 Hugging Face LFS object ID 完全一致。
- 已验证 backend：NVIDIA vLLM container `nvcr.io/nvidia/vllm:26.03-py3`，
  Python 3.12.3、PyTorch `2.11.0a0+a6c236b9fd.nv26.03.46836102`、vLLM
  `0.17.1+a03ca76a.nv26.03.46967107`、Transformers 4.57.5，以及
  `mineru-vl-utils==1.0.5`；engine 启动时加载
  `mineru_vl_utils:MinerULogitsProcessor`。
- 健康检查：`/v1/models`、`/health` 均返回 HTTP 200。
- `simple_text_1p.pdf`：CLI exit 0、document success、一个
  `success_fallback_text` page；parser 4.520 秒、wall time 7.558 秒；正文可读且
  基本正确，但 Checklist 重复一次。
- `receipt_narrow_1p.pdf`：CLI exit 0、document success、一个
  `success_fallback_text` page；parser 3.407 秒、wall time 4.428 秒；收据字段和
  金额可读且基本正确，但表格被展平为连续文本。
- 资源观测：`gpu_memory_utilization=0.40`；日志显示模型权重 2.16 GiB、KV cache
  43.4 GiB、预算上限约 47.9 GiB，低于本次验证限制，但该数据不是 benchmark。
- 负向对照：通用 SGLang 路径虽然 HTTP 健康且 CLI success，却生成了 4096 个重复
  `!`。直接用同一页面和 `Layout Detection:` prompt 对照时，SGLang 在请求
  `max_tokens=64` 后以 `finish_reason=length` 返回 64/64 个 `!`；带
  `mineru_vl_utils:MinerULogitsProcessor` 的 vLLM 在请求 `max_tokens=512` 后以
  `finish_reason=stop` 返回 312 个有效 layout token。SGLang 没有加载必需的
  logits processor，因此不认证该模型 revision 的 SGLang。

正常 MinerU 两阶段输出也被统一记录为 `success_fallback_text`，因此在 sidecar 能够
区分正常成功和实际 fallback 前，该 backend 只能保持条件通过。

## PaddleOCR-VL 证据

- Parser：`v0.2.0`，commit `9c3bea6`。
- 识别模型：`PaddleOCR-VL-1.6`，固定权重 revision
  `d911116c363676c602c4786ad0b9667b1aee055f`，`model.safetensors` SHA256
  `85a479d506a11e724e7285d395c551be69f41dbc16b6342d3cacfb189aed71db`，
  model card 声明 Apache-2.0。
- Layout 模型：`PP-DocLayoutV2`，revision
  `a0a882d03564ff3a9c9b37e48e2e636e7c236fd6`，`model.safetensors` SHA256
  `e60f3725aeedc88fd319416ef166bda79171a41516a301c27cab9132dc2739d2`；model card 声明
  Apache-2.0。本地 snapshot 没有独立 LICENSE 文件，下游打包必须保留 model card
  证据。
- 实测 recognition runtime：Python 3.12.13、PyTorch `2.11.0+cu130`、
  Transformers 5.5.4、SGLang source commit
  `0fe2dbd42caeb627bd8aca162dab7763d292fda9`、sglang-kernel 0.4.2.post2 和
  flashinfer-python 0.6.7.post3。共享环境存在依赖漂移并使用了 Triton/PyTorch
  backend 绕开 kernel 版本不匹配，因此该环境不是可复现的生产锁定镜像。
- 健康检查：VLM `/v1/models`、layout `/health`、layout `/detect` 和真实 chat
  completions 均返回 HTTP 200。
- `simple_text_1p.pdf`：CLI exit 0、document success、一个
  `success_fallback_text` page；parser 耗时 2.366 秒，wall time 4.102 秒；正文可读，
  有少量符号误识别。
- `receipt_narrow_1p.pdf`：CLI exit 0、document success、一个
  `success_fallback_text` page；parser 耗时 1.670 秒，wall time 2.455 秒；正文可读，
  但检测出的表格包含空单元格。
- 资源观测：识别服务日志显示约 45.78 GiB reserved/used，低于约 60 GiB 验证预算；
  layout service 未单独暴露显存，因此该数据不是 benchmark。
- 清理：本任务启动的 MinerU、Paddle VLM 与 layout 服务均已停止，验证端口不再
  监听，GPU compute process 为空；原有共享 mock 服务未停止。

v0.3 开发主线为了兼容仍保留 `success_fallback_text`，但已经新增 `stages` 和结构化
`fallback` 元数据。正常 MinerU/Paddle 两阶段完成记录 `fallback.used=false`，真实
降级记录受控的 reason 和 source stage。认证仍保持条件通过，直到 v0.3 release
candidate 使用锁定的真实服务完成复验。

## 每个引擎的最低证据

每个真实服务记录必须包含 parser commit、公开 fixture checksum、固定模型 revision
和许可证、runtime 版本、脱敏参数、健康与退出结果、page/fallback 状态、产物完整性、
耗时与 GPU 显存观测、人工抽查、已知限制和清理确认。

认证证据不得公开内网主机、凭据、私有模型路径、客户文档或共享机器进程细节。

## 发布与部署策略

核心发布门禁包括 CI、行为契约、PostgreSQL migration/并发检查、mock 端到端、
wheel 安装、输出审计和性能回退保护，不启动 GPU 模型服务。

生产启用还必须要求准确模型/profile 对应的当前 **Certified** 记录；如果只有
**Verified**，则必须明确记录风险接受。模型 revision、服务 major version、parser
输出契约或关键部署拓扑变化后，旧证据自动失效，需要重新执行受影响检查。
