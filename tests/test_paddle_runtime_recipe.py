import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ROOT / "deploy" / "engines" / "paddleocr-vl"


def test_paddle_arm64_recipe_pins_runtime_and_model_provenance():
    lock = json.loads((RUNTIME_DIR / "runtime.lock.json").read_text(encoding="utf-8"))
    dockerfile = (RUNTIME_DIR / "Dockerfile.arm64").read_text(encoding="utf-8")

    assert lock["architecture"] == "linux/arm64"
    assert lock["base_image_digest"].startswith("sha256:")
    assert len(lock["base_image_digest"]) == len("sha256:") + 64
    assert len(lock["sglang_revision"]) == 40
    assert len(lock["sglang_source_sha256"]) == 64
    assert len(lock["model_revision"]) == 40
    assert len(lock["layout_model_revision"]) == 40
    assert len(lock["model_safetensors_sha256"]) == 64
    assert lock["sglang_kernel"] == "0.4.4"

    for value in (
        lock["base_image_digest"],
        lock["sglang_revision"],
        lock["sglang_source_sha256"],
        lock["sglang_kernel"],
        lock["transformers"],
        lock["flashinfer_python"],
        lock["model_revision"],
    ):
        assert value in dockerfile
    assert "test \"$(uname -m)\" = \"aarch64\"" in dockerfile
    assert "ENTRYPOINT" in dockerfile


def test_paddle_runtime_readme_requires_digest_fixture_and_weight_checks():
    text = (RUNTIME_DIR / "README.md").read_text(encoding="utf-8")
    prepare = (RUNTIME_DIR / "prepare-build-context.sh").read_text(encoding="utf-8")

    assert "docker image inspect" in text
    assert "sha256sum" in text
    assert "check_engine_fixture_outputs.py" in text
    assert "does not include model weights" in text
    assert "--retry 5" in prepare
    assert "shasum -a 256" in prepare
