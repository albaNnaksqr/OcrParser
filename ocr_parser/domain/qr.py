from __future__ import annotations

import contextlib
import io


def _is_qr_or_barcode(self, image) -> bool:
    if not self.qr_scanner_available:
        return False

    try:
        import cv2
        import numpy as np
        from pyzbar import pyzbar
        from pyzbar.pyzbar import ZBarSymbol
    except Exception:
        return False

    is_qr = False
    try:
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        cv2_image = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        h, w = cv2_image.shape[:2]
        if min(h, w) <= 40:
            return False
        try:
            qr = cv2.QRCodeDetector()
            ok, _, points, _ = qr.detectAndDecodeMulti(cv2_image)
            if ok and points is not None and len(points) > 0:
                is_qr = True
        except Exception:
            try:
                _, pts, _ = qr.detectAndDecode(cv2_image)
                if pts is not None and len(pts) > 0:
                    is_qr = True
            except Exception:
                pass

        if not is_qr:
            try:
                gray_image = cv2.cvtColor(cv2_image, cv2.COLOR_BGR2GRAY)
                _, binary_image = cv2.threshold(gray_image, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                stderr_capture = io.StringIO()
                decoded_objects = []
                with contextlib.redirect_stderr(stderr_capture):
                    decoded_objects = pyzbar.decode(
                        binary_image,
                        symbols=[
                            ZBarSymbol.QRCODE,
                            ZBarSymbol.CODE128,
                            ZBarSymbol.EAN13,
                            ZBarSymbol.EAN8,
                            ZBarSymbol.UPCA,
                            ZBarSymbol.UPCE,
                            ZBarSymbol.CODE39,
                            ZBarSymbol.CODE93,
                        ],
                    )
                if decoded_objects and "Assertion" not in stderr_capture.getvalue():
                    is_qr = True
            except Exception:
                pass
    except Exception as exc:
        self._console_write(f"Warning: QR/barcode detection failed with an unexpected error: {exc}", level="warning")
        return False
    return is_qr
