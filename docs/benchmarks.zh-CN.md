# Benchmark 说明

[English](benchmarks.md) | 中文

本文总结 OcrParser 开发过程中的脱敏 benchmark 证据。这些数字用于说明框架行为，
不用于给 OCR 模型做排行榜。

不同 run 的资源预算不同：

- DotsOCR 验证使用了更大的 API 并发预算，并针对高吞吐批处理调优。
- MinerU-style 和 PaddleOCR-VL-style 验证使用较小的 two-stage smoke/gray runs，
  用来证明 layout/recognition 行为有边界且可观测。
- Endpoint queueing、硬件、PDF 形态和 OCR 模型质量没有跨 engine 归一化。

## 如何在你的 Endpoint 上复现

生成 synthetic fixtures：

```bash
python3 tools/generate_benchmark_pdfs.py --output-dir /tmp/ocr-benchmark-pdfs
```

运行目录 benchmark：

```bash
python3 tools/run_performance_baseline.py \
  --input-dir /tmp/ocr-benchmark-pdfs \
  --output-root /tmp/ocr-benchmark-results \
  --variant current=. \
  --run-mode directory \
  --engine dotsocr \
  --ip YOUR_MODEL_ENDPOINT \
  --port 13080 \
  --model-name DotsOCR \
  --file-concurrency 4 \
  --page-concurrency 16
```

runner 会写出 CSV 和 Markdown summaries，包含 duration、pages per second、
status、output paths，以及日志中可提取的 runtime metrics。

v0.2 发布候选需要在同一环境运行基线与候选版本，并执行 10% mock 吞吐回退门禁：

```bash
python3 tools/check_performance_regression.py \
  /tmp/ocr-benchmark-results/results.csv \
  --baseline-variant baseline \
  --candidate-variant current \
  --max-regression-percent 10
```

## DotsOCR：页级并发曲线

Synthetic fixture set：

- 8 个 PDF
- 总计 36 页
- 单 parser process
- `num_cpu_workers=1`
- `md_gen_concurrency=1`
- 关闭 resume，并强制重新处理

| Page concurrency | Files OK | Pages | Total s | Avg s/page | Speedup vs c=1 | Long document speedup |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 8/8 | 36 | 193.612 | 5.378 | 1.00x | 1.00x |
| 2 | 8/8 | 36 | 131.533 | 3.654 | 1.47x | 1.59x |
| 4 | 8/8 | 36 | 81.911 | 2.275 | 2.36x | 3.17x |
| 8 | 8/8 | 36 | 79.364 | 2.205 | 2.44x | 4.64x |
| 16 | 8/8 | 36 | 64.241 | 1.784 | 3.01x | 7.42x |

解读：

- DotsOCR-style 全页 VLM 抽取在多页文件上明显受益于客户端页级并发。
- 单页和双页文件更容易受固定开销影响，因此波动更明显。
- 保守的生产起点应该同时考虑 endpoint queue health 和 failure rate，
  不能只看最快一次本地 timing。

## DotsOCR：文件级并发和全局 API 池

核心调度问题是：目录任务应该一次只处理一个 PDF，还是让多个 PDF 共享一个有界全局 API 池。

Small A/B：

| Files | Pages | Global API cap | File concurrency | Duration | Throughput |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 5 | 251 | 80 | 1 | 79.11s | 3.17 page/s |
| 5 | 251 | 80 | 3 | 50.17s | 5.00 page/s |

`file_concurrency=3` 将 wall time 降低 28.94s，并在该样本上提升约 58% 吞吐。

Medium sweep：

| Files | Pages | Global API cap | Best tested file concurrency | Best throughput |
| ---: | ---: | ---: | ---: | ---: |
| 16 | 1156 | 80 | 5 | 5.91 page/s |
| 50 | 2969 | 80 | 8, stable repeat | 7.82 page/s |

解读：

- 文件级并发可以在单个 PDF 经历 render、API 和 post-processing 阶段时提升整体利用率。
- 当 API pool 已经饱和后，更高 file concurrency 不会提高 API ceiling。
- Tail latency 和 post-processing 可能主导 run 的最后阶段，所以最佳值与 workload 相关。

## Platform 路径开销

同一个 50 PDF、2969 页 DotsOCR gray workload 也通过 control/agent/shard 路径运行过：

| Path | Files | Pages | Settings | Duration | Throughput |
| --- | ---: | ---: | --- | ---: | ---: |
| Direct CLI repeat | 50 | 2969 | API cap 80, `file_concurrency=8` | 379.70s | 7.82 page/s |
| Control/agent/shard | 50 | 2969 | Same parser profile, one shard | 376.07s | 7.90 page/s |

解读：

- Platform 路径把同一 parser profile 传给了 worker。
- 在这个 gray-test 规模下，control API、agent loop、manifest scan 和 shard execution
  没有带来明显吞吐开销。
- 这个结果不证明可以无限扩展；它说明在该 workload 形态下，orchestration layer
  不是瓶颈。

## MinerU-Style 两阶段验证

MinerU-style parsing 对 layout 和 block recognition 都使用 VLM endpoint。
这意味着单个 global API cap 不够；layout 需要保留容量，避免 recognition 饿死新页面。

Medium gray run：

| Files | Pages | File concurrency | Page concurrency | API cap | Recognition cap | Result |
| ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 50 | 50 | 4 | 4 | 8 | 6 | 50/50 success, no API errors/timeouts |

观察到的行为：

- layout calls 和 recognition calls 分开计数；
- recognition queue depth 可见；
- block filtering 减少了不必要的 recognition work；
- 每个文件都有非空 output artifacts。

## PaddleOCR-VL-Style 两阶段验证

PaddleOCR-VL-style parsing 将 layout detection 与 VLM block recognition 分离。
稳定性风险是 layout 跑得太快，制造过大的 block backlog。

Medium gray run：

| Files | Pages | File concurrency | Page concurrency | API cap | Layout cap | Result |
| ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 50 | 50 | 4 | 4 | 8 | 2 | 50/50 success, no API errors or layout fallbacks |

观察到的行为：

- layout calls 与 recognition calls 分别限流；
- block queue depth 可见；
- high/low watermark backpressure 可用；
- 每个文件都有非空 output artifacts。

## 如何解读这些结果

最强的结论是框架层面的：

- concurrency 必须按 engine 和 endpoint 调优；
- 对 two-stage engine 来说，单个全局“同时处理多少页”的旋钮不够；
- 当 endpoint 有 headroom 时，有界文件级并发可以提升批处理吞吐；
- 只要正确传递同一 profile，control/worker 路径可以保持 parser 吞吐；
- 可观测性 counters 是性能故事的一部分，因为它们能显示瓶颈在 API、layout、
  recognition、CPU 还是尾部 output work。
