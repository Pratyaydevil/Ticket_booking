"""
QR ticket generation. The QR encodes the booking reference (spec requirement)
so gate staff can scan it and verify via GET /api/verify/{booking_ref}.
"""
import io

import qrcode


def qr_png_bytes(booking_ref: str) -> bytes:
    """Render the booking reference as a PNG QR code and return raw bytes."""
    img = qrcode.make(booking_ref, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
