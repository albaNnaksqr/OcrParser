"""
Layout Detection Service — wraps PP-DocLayoutV2 (pure PyTorch, no PaddlePaddle).

POST /detect
  body: { "image_b64": "<base64-encoded image>", "use_paddlex_filter_boxes": true }
  resp: { "boxes": [{"bbox": [x1,y1,x2,y2], "label": "text", "score": 0.95, "index": 0}] }

GET /health  →  200 {"status": "ok"}
"""
from __future__ import annotations

import base64
import io
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import List, Optional

import torch
from fastapi import FastAPI, HTTPException
from PIL import Image
from pydantic import BaseModel

# Make local copies of model code importable
sys.path.insert(0, os.path.dirname(__file__))
from pp_doclayoutv2 import PPDocLayoutV2LayoutModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

MODEL_PATH = os.environ.get("LAYOUT_MODEL_PATH", "")
DEVICE = os.environ.get("LAYOUT_DEVICE", "cuda" if torch.cuda.is_available() else "cpu")
CONF = float(os.environ.get("LAYOUT_CONF", "0.3"))

_model: Optional[PPDocLayoutV2LayoutModel] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _model
    if not MODEL_PATH:
        raise RuntimeError("LAYOUT_MODEL_PATH env var is not set")
    log.info("Loading PP-DocLayoutV2 from %s on %s ...", MODEL_PATH, DEVICE)
    _model = PPDocLayoutV2LayoutModel(weight=MODEL_PATH, device=DEVICE, conf=CONF)
    log.info("Model loaded.")
    yield
    _model = None


app = FastAPI(title="Layout Detection Service", lifespan=lifespan)


class DetectRequest(BaseModel):
    image_b64: str
    use_paddlex_filter_boxes: bool = True


class Box(BaseModel):
    bbox: List[float]
    label: str
    score: float
    index: int


class DetectResponse(BaseModel):
    boxes: List[Box]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/detect", response_model=DetectResponse)
def detect(req: DetectRequest):
    if _model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    try:
        image_bytes = base64.b64decode(req.image_b64)
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid image: {e}")

    results = _model.predict(image, use_paddlex_filter_boxes=req.use_paddlex_filter_boxes)
    boxes = [
        Box(
            bbox=r["bbox"],
            label=r["label"],
            score=float(r["score"]),
            index=int(r["index"]),
        )
        for r in results
    ]
    return DetectResponse(boxes=boxes)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("LAYOUT_PORT", "30002"))
    uvicorn.run(app, host="0.0.0.0", port=port)
