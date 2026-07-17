# v0.2 发布检查表

在干净的公开仓库 checkout 中执行：

```bash
python -m compileall -q ocr_parser dots_ocr ocr_platform tools
python -m pytest -q
python tools/check_docs_links.py
python tools/run_mock_e2e.py
python -m build --wheel
```

CI 的 PostgreSQL job 会应用全部 migration，并验证并发 shard/scan-unit claim；
package job 会在空环境安装 wheel，验证三个 console script 声明以及 UI/migration
package data。

Parser 打 tag 前必须：

- 使用 `tools/check_performance_regression.py` 比较基线与候选 CSV，吞吐回退不超过 10%；
- 在发布环境验证停止/恢复、Worker 断线 spool/replay、manifest integrity 和输出审计；
- 确认 `git status --short` 为空，全部必需 CI job 通过；
- 内网部署只消费这一个准确 commit 的 tag。
- 确认 wheel 包含 `AGPL_3.0.txt`、`LICENSE` 和 `NOTICE`；
- 开启 API auth 后启动 Control，确认 `/source`、`/source.json` 和
  `/legal/agpl-3.0` 仍可公开访问；
- 确认 `/source` 指向准确 tag commit。未打 tag 或带补丁的 build 必须在开放服务前
  设置 `OCR_PLATFORM_SOURCE_REVISION` 或 `OCR_PLATFORM_SOURCE_URL`。

真实模型检查单独维护在[引擎认证矩阵](engine-certification.zh-CN.md)中。它是生产启用
对应引擎时的部署门禁，不要求在创建 GitHub Release 时启动 GPU 服务，也不能替代
单元测试。
