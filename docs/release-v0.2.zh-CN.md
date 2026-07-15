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

打 tag 前必须：

- 使用 `tools/check_performance_regression.py` 比较基线与候选 CSV，吞吐回退不超过 10%；
- 在发布环境验证停止/恢复、Worker 断线 spool/replay、manifest integrity 和输出审计；
- 使用 DotsOCR、MinerU、PaddleOCR-VL 服务人工检查小型公开 PDF 的质量；
- 确认 `git status --short` 为空，全部必需 CI job 通过；
- 内网部署只消费这一个准确 commit 的 tag。

真实模型质量验证需要模型服务和 GPU，因此保留为人工发布门禁，不能用单元测试替代。
