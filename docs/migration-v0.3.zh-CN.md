# 从 v0.2 迁移到 v0.3

[English](migration-v0.3.md)

v0.3 保持 CLI 参数与退出码、HTTP 路径与 schema、Job/Shard 状态机、0001-0018
migration、manifest JSONL 和输出格式。唯一有意改变的安装语义是：默认 wheel 只包含
Parser 和远程引擎 client。

## 选择安装 profile

| 使用场景 | 安装命令 |
| --- | --- |
| Parser 与远程 OCR 服务 | `pip install ocrparser-platform` |
| Control、Agent、PostgreSQL | `pip install 'ocrparser-platform[platform]'` |
| S3 helper | `pip install 'ocrparser-platform[s3]'` |
| 本地 PP-DocLayout service | `pip install 'ocrparser-platform[layout]'` |
| 等价于 v0.2 的非 GPU runtime | `pip install 'ocrparser-platform[full]'` |
| 贡献者环境 | `pip install 'ocrparser-platform[dev]'` |

`full` 不安装硬件相关的 layout GPU runtime。原 console script 名称全部保留；缺少
extra 时会给出准确安装命令，不暴露原始 import traceback。

## 升级数据库

0001-0018 migration 不变，0019 为历史记录回填 checksum。生产部署应在重启 Control
前执行统一 runner：

```bash
ocr-platform-migrate plan --database-url "$OCR_PLATFORM_DATABASE_URL"
ocr-platform-migrate apply --database-url "$OCR_PLATFORM_DATABASE_URL"
ocr-platform-migrate verify --database-url "$OCR_PLATFORM_DATABASE_URL"
```

v0.3 仍保留 startup 自动升级，但推荐生产使用显式 CLI。

## 核对兼容与安全

- `ocr_platform.control.service` 在 v0.3 保留兼容 façade；新集成应从对应 Control
  domain 导入；
- 旧 `success_fallback_text` 和 `success_fallback_image` 继续有效，消费者应迁移到
  结构化 `stages` 与 `fallback` metadata；
- Control 继续默认 loopback，Remote Admin 继续 opt-in，浏览器 token 只在 session
  保存，数据库 model key 默认禁止；
- 正式部署应确认 `/source.json` 显示准确 wheel revision 且
  `release_build=true`。
