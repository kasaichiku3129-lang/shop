"""その場撮影して Streamlit に画像を返すカメラコンポーネント。"""

from __future__ import annotations

import base64
import io
from pathlib import Path

import streamlit.components.v1 as components
from PIL import Image, ImageOps

_COMPONENT_DIR = Path(__file__).parent / "frontend" / "web"
_invoice_camera_component = components.declare_component(
    "invoice_camera_capture",
    path=str(_COMPONENT_DIR.resolve()),
)


def capture_invoice_camera_image(*, key: str | None = None) -> Image.Image | None:
    """ブラウザ内カメラで撮影し、PIL Image を返す。未撮影なら None。"""
    result = _invoice_camera_component(key=key, default=None)
    if isinstance(result, str) and result.startswith("data:image"):
        _header, encoded = result.split(",", 1)
        img = Image.open(io.BytesIO(base64.b64decode(encoded)))
        img = ImageOps.exif_transpose(img).convert("RGB")
        return _ensure_invoice_min_resolution(img)
    return None


def _ensure_invoice_min_resolution(img: Image.Image, min_short_edge: int = 1200) -> Image.Image:
    """横長の低解像度キャプチャを OCR 向けに最低解像度まで拡大する。"""
    w, h = img.size
    short_edge = min(w, h)
    if short_edge >= min_short_edge:
        return img
    scale = min_short_edge / short_edge
    new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
    return img.resize(new_size, Image.Resampling.LANCZOS)
