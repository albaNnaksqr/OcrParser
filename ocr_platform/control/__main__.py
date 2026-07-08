from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("OCR_PLATFORM_HOST", "0.0.0.0")
    port = int(os.environ.get("OCR_PLATFORM_PORT", "8080"))
    uvicorn.run("ocr_platform.control.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
