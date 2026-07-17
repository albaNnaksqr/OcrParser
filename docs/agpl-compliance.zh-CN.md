# AGPL 合规

[English](agpl-compliance.md) | 中文

本文是工程合规流程，不构成法律意见。OcrParser 原创源码继续按 MIT 提供。
PyMuPDF 是项目直接导入的必需依赖，Artifex 以 GNU AGPLv3 或商业许可提供它。
安装 AGPL build 时，组合应用按 GNU AGPLv3 提供，同时保留 MIT、Apache-2.0 和
其他独立许可部分的原有声明。

## 网络源码入口

Control service 在 API token middleware 之外公开以下路由：

- `/source`：跳转到当前运行版本的 Corresponding Source；
- `/source.json`：记录 package version、源码 revision、URL、许可证和无担保声明；
- `/legal/agpl-3.0`：直接提供随包分发的完整 GNU AGPLv3 文本。

Control UI 显著展示 copyright、无担保、再分发、源码和许可证入口。即使启用
`OCR_PLATFORM_API_TOKEN`，所有网络用户仍必须能够访问这些路由。

正式 tag wheel 默认把 `/source` 指向与 package version 对应的仓库 tag，例如
`v0.2.1`。未打 tag、带补丁或内部自行构建的部署必须通过以下一种方式固定准确源码：

```bash
OCR_PLATFORM_SOURCE_REVISION=<准确的公开 commit>
# 或使用不可变的源码归档：
OCR_PLATFORM_SOURCE_URL=https://downloads.example/source/ocrparser-<commit>.tar.gz
```

`OCR_PLATFORM_SOURCE_URL` 优先级更高。该地址必须免费、无需 token，并在许可证要求的
分发和支持周期内持续可用。带补丁的部署不得指向旧 release tag，也不能只指向未说明
实际部署 commit 的移动分支。

wheel 还会写入准确 build revision、timestamp 和 dirty state。没有显式部署覆盖时，
`/source.json` 使用该 revision，不再只根据 package version 推断源码。
`release_build=false` 或 `build_dirty=true` 表示这不是正式发布构建。

## Corresponding Source 边界

源码地址必须包含准确运行版本的 OcrParser 源码，以及构建、安装、运行和修改所需的
非秘密材料，包括依赖声明、container/build 文件、运维脚本和本地补丁。所有 copyright、
修改、许可证和无担保声明都必须保留。

凭据、客户文档、数据库内容、内网主机名和其他部署秘密不是源码，不得公开。独立模型
服务和模型权重继续遵守自己的许可证及引擎认证记录。

## 部署验证

开放网络访问前执行：

```bash
curl -fsS http://CONTROL_HOST:CONTROL_PORT/source.json
curl -fsS http://CONTROL_HOST:CONTROL_PORT/legal/agpl-3.0 | head
curl -sSI http://CONTROL_HOST:CONTROL_PORT/source
```

确认跳转无需认证即可取得准确运行源码，并且该源码能够复现部署应用。发布 wheel 必须
包含 `LICENSE`、`NOTICE` 和 `third_party/licenses/AGPL_3.0.txt`；CI 会检查该清单。

如果部署方持有有效的 Artifex 商业许可，则 PyMuPDF 使用改由该协议约束。商业协议和
适用 notice 策略只记录在私有部署清单中，不要把协议或凭据提交到本仓库。
