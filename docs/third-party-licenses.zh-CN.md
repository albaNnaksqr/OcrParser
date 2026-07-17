# 第三方许可证审计

[English](third-party-licenses.md) | 中文

本文是工程侧许可证清单，不构成法律意见。仓库顶层 MIT License 只适用于
OcrParser 原创代码；仓库内直接包含或修改的第三方代码，以及单独安装的运行时
依赖，仍适用各自许可证。

审计日期：2026-07-17。

## 仓库内包含或修改的源码

| 组件 | 本仓库代码 | 上游与核对来源 | 许可证 | 当前处理 |
| --- | --- | --- | --- | --- |
| dots.ocr | `dots_ocr/**` | [rednote-hilab/dots.ocr](https://github.com/rednote-hilab/dots.ocr) | MIT | 已在 `NOTICE` 和 `third_party/licenses/DOTS_OCR_LICENSE.txt` 保留版权与完整 MIT 文本。 |
| PaddleX | `ocr_parser/engines/paddleocr_vl.py` 中的跨页表格合并与标题层级 helper | PaddleX v3.5.1 [`merge_table.py`](https://github.com/PaddlePaddle/PaddleX/blob/v3.5.1/paddlex/inference/pipelines/layout_parsing/merge_table.py) 和 [`title_level.py`](https://github.com/PaddlePaddle/PaddleX/blob/v3.5.1/paddlex/inference/pipelines/layout_parsing/title_level.py) | Apache-2.0 | 已保留版权、来源、修改声明和完整 Apache-2.0 文本。 |
| MinerU | `services/layout_detection/bbox_utils.py`、`services/layout_detection/pp_doclayoutv2.py` | [MinerU commit `e52d40b`](https://github.com/opendatalab/MinerU/tree/e52d40b51ef76db5d057d84412a9d79d7aff744f/mineru/model/layout) | 该导入快照为 Apache-2.0 | 已保留版权、准确 source revision、修改声明和完整 Apache-2.0 文本；当前 MinerU 模型和更新源码需要单独审查。 |

历史仓库显示这两个 layout 文件于 2026-05-05 导入。`bbox_utils.py` 与 MinerU
commit `e52d40b51ef76db5d057d84412a9d79d7aff744f` 的 Git blob 完全一致；
`pp_doclayoutv2.py` 只修改了本地 import path，并增加两处已记录的 Transformers 5
兼容修复。该准确 commit 的[许可证](https://github.com/opendatalab/MinerU/blob/e52d40b51ef76db5d057d84412a9d79d7aff744f/LICENSE.md)
是 Apache-2.0。

## 当前 MinerU 模型与更新源码

MinerU 在该导入快照之后修改了仓库许可证。当前 MinerU Open Source License 以
Apache-2.0 为基础并增加额外条款。使用当前 MinerU 模型或更新源码时，必须按其
自身声明的条款重新审查，不能直接沿用旧快照的 Apache-2.0 结论。当前附加条款包括：

- 商业使用超过许可证写明的 MAU 或月收入任一门槛时，需要另行取得商业许可；
- 基于 MinerU 向第三方提供在线服务时，必须在产品界面或公开文档显著标明使用了
  MinerU；
- 部署前必须复核上游权威版本的许可文本。

当前条款应以 MinerU 上游的[权威许可证](https://github.com/opendatalab/MinerU/blob/master/LICENSE.md)
为准。如果这些条款适用，本公开页面完成了项目级 attribution；下游产品仍需确认
自己的 UI 或公开文档满足在线服务标识要求。

## PyMuPDF AGPL 部署路径

`PyMuPDF==1.26.3` 是必需依赖，项目直接用它打开、渲染 PDF 和生成输出。
PyMuPDF 采用 GNU AGPL v3 / Artifex 商业许可双许可证；仓库自身的 MIT License
不会消除组合部署时的这些义务。

公开仓库现在已经实现 AGPL 部署路径：

- 完整 GNU AGPLv3 文本随源码和 wheel 分发在
  `third_party/licenses/AGPL_3.0.txt`；
- `LICENSE` 和 `NOTICE` 说明组合部署及继续保留的宽松许可证声明；
- Control 公开 `/source`、`/source.json`、`/legal/agpl-3.0`，UI 显示法律和源码入口；
- 正式 build 默认解析到相同版本 tag，带补丁的 build 可固定到准确公开 commit 或
  不可变源码归档。

部署验证见 [AGPL 合规](agpl-compliance.zh-CN.md)。部署方仍然可以选择其他路径：

1. 从 Artifex 获得并记录适用的商业许可；
2. 用符合部署许可策略的库替换 PyMuPDF，并重新执行 PDF 行为契约和性能基线。

参见 [PyMuPDF 官方许可证说明](https://pymupdf.readthedocs.io/en/latest/about.html)。

## 模型权重

本仓库不分发 DotsOCR、MinerU、PaddleOCR-VL 或 layout 模型权重。模型仓库的
许可证可能与 parser、推理服务和 layout service 源码许可证不同，因此引擎认证会
分别记录代码许可与模型许可。参见[引擎认证](engine-certification.zh-CN.md)。

## 发布检查

每次发布如果新增或升级复制源码、依赖或模型 profile，必须：

- 记录准确的上游仓库、tag/commit、文件路径和导入日期；
- 保留上游版权、许可证、NOTICE 和本地修改声明；
- 确认源码包和 wheel 都包含 `third_party/licenses/*`；
- 将运行时依赖许可证与仓库内源码许可证分开审查；
- 在引擎认证证据中记录准确的模型仓库、revision 和许可证；
- 验证 `/source` 无需认证并解析到准确部署源码。
