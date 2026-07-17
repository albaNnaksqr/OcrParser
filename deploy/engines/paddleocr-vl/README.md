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
and can be removed after the build. The runtime imports SGLang directly from
that immutable source tree; it does not build the unrelated optional Rust
extension into a wheel.

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
build is not by itself an engine-quality certification.

## 中文

该配方复现 v0.3.1 认证使用的 NVIDIA Spark ARM64 PaddleOCR-VL recognition
service。镜像不包含模型权重或 PP-DocLayout；运行时必须挂载准确本地 revision，并核对
对应模型许可证。

构建后必须记录不可变 image digest，并在启动前使用 `runtime.lock.json` 中的 SHA256
验证权重。服务只绑定 loopback，GPU budget 保持在共享机器限制以内。

完成认证还需要记录 `/health`、`/v1/models`、准确 parser commit、layout model
revision，并让 `tools/check_engine_fixture_outputs.py` 的公开 fixture 报告通过。镜像
构建成功本身不代表引擎质量已经认证。
