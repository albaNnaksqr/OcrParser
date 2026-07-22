# PaddleOCR-VL ARM64 Runtime

English | [中文](#中文)

This recipe reproduces the PaddleOCR-VL recognition service used by the v0.3.1
certification work on NVIDIA Spark ARM64. It does not include model weights or
PP-DocLayout. Review the model licenses and mount exact local revisions at
runtime. The SGLang source archive is pinned by both commit and SHA256 so the
build fails closed if the fetched source changes.

Build and record the immutable result:

```bash
bash deploy/engines/paddleocr-vl/prepare-build-context.sh
docker build \
  --file deploy/engines/paddleocr-vl/Dockerfile.arm64 \
  --tag ocrparser/paddleocr-vl:0.3.1-arm64 \
  .
docker image inspect ocrparser/paddleocr-vl:0.3.1-arm64 \
  --format '{{index .RepoDigests 0}} {{.Id}}'
```

When the official PyPI endpoint is unavailable, `--build-arg
PIP_INDEX_URL=https://<trusted-mirror>/simple` may select an operator-approved
mirror. The post-install assertions still enforce the locked package versions.

The preparation script downloads the exact commit archive with retries and
verifies its SHA256 before Docker receives it. The archive is ignored by Git
and can be removed after the build. The runtime imports SGLang Python code from
that immutable source tree and builds `sglang-kernel==0.4.4` from the matching
`sgl-kernel/` source with `scikit-build-core==0.11.6`; it does not install the
PyPI kernel wheel. The image build fails unless
`sgl_kernel/sm100/common_ops*` is present. On CUDA 13 ARM64, that extension
contains the SM121 gencode used by compute capability 12.1.

The pinned NVIDIA base and `flashinfer-python==0.6.7.post3` expose different
FlashInfer build-version strings. This recipe therefore sets
`FLASHINFER_DISABLE_VERSION_CHECK=1` explicitly. This is a constrained
compatibility strategy for this exact base composition, not a general safety
recommendation and not evidence for Certified status.

Run a GPU extension import smoke before starting the service:

```bash
docker run --rm --gpus all \
  --entrypoint python \
  ocrparser/paddleocr-vl:0.3.1-arm64 \
  -c 'import importlib, torch; assert torch.cuda.get_device_capability() == (12, 1); m = importlib.import_module("sgl_kernel.sm100.common_ops"); print(m.__file__)'
```

Before startup, verify the mounted weight file against `runtime.lock.json`.
Then bind the API to loopback and keep the GPU budget below the shared-machine
limit:

```bash
test "$(sha256sum /models/PaddleOCR-VL-1.6/model.safetensors | cut -d' ' -f1)" = \
  85a479d506a11e724e7285d395c551be69f41dbc16b6342d3cacfb189aed71db

docker run --rm --gpus all --shm-size=16g \
  --name ocrparser-paddleocr-vl-cert \
  --publish 127.0.0.1:30001:30001 \
  --volume /models/PaddleOCR-VL-1.6:/model:ro \
  ocrparser/paddleocr-vl:0.3.1-arm64 \
  --model-path /model \
  --served-model-name paddleocr-vl \
  --host 0.0.0.0 \
  --port 30001 \
  --trust-remote-code \
  --context-length 32768 \
  --mem-fraction-static 0.40 \
  --attention-backend triton \
  --sampling-backend pytorch
```

Certification requires the built image digest, `/health` and `/v1/models`, the
exact parser commit, the layout model revision, and a passing public-fixture
report from `tools/check_engine_fixture_outputs.py`. A successful container
build is not by itself an engine-quality certification. `docker image inspect`
may return only an image ID for an image that has not been pushed. Until a
registry-backed immutable RepoDigest is recorded, this runtime can be no more
than **Verified / limited**.

## 中文

该配方复现 v0.3.1 认证使用的 NVIDIA Spark ARM64 PaddleOCR-VL recognition
service。镜像不包含模型权重或 PP-DocLayout；运行时必须挂载准确本地 revision，并核对
对应模型许可证。

准备脚本固定 SGLang source revision 与 archive SHA256。镜像从同一 source tree 的
`sgl-kernel/` 构建 `sglang-kernel==0.4.4`，不会安装 PyPI kernel wheel；构建时必须找到
`sgl_kernel/sm100/common_ops*`。运行前还必须在 compute capability 12.1 GPU 上执行
上面的 extension import smoke。

固定 NVIDIA base 与 `flashinfer-python==0.6.7.post3` 的 build-version string 不一致，
因此该配方显式设置 `FLASHINFER_DISABLE_VERSION_CHECK=1`。这只是准确固定 base 组合的
受限兼容策略，不是通用建议，也不能作为 Certified 证据。

构建后必须记录不可变 RepoDigest，并在启动前使用 `runtime.lock.json` 中的 SHA256 验证
权重。只有本地 image ID、没有 registry RepoDigest 时，状态最多为
**Verified / limited**。服务只绑定 loopback，GPU budget 保持在共享机器限制以内。

完成认证还需要记录 `/health`、`/v1/models`、准确 parser commit、layout model
revision，并让 `tools/check_engine_fixture_outputs.py` 的公开 fixture 报告通过。镜像
构建成功本身不代表引擎质量已经认证。
