"""その場撮影して Streamlit に画像を返すカメラコンポーネント。"""

from __future__ import annotations

import base64
import io
from pathlib import Path

import streamlit.components.v1 as components
from PIL import Image

_COMPONENT_DIR = Path(__file__).parent / "frontend" / "build"
_invoice_camera_component = components.declare_component(
    "invoice_camera_capture",
    path=str(_COMPONENT_DIR.resolve()),
)


def capture_invoice_camera_image(*, key: str | None = None) -> Image.Image | None:
    """ブラウザ内カメラで撮影し、PIL Image を返す。未撮影なら None。"""
    result = _invoice_camera_component(key=key, default=None)
    if isinstance(result, str) and result.startswith("data:image"):
        _header, encoded = result.split(",", 1)
        return Image.open(io.BytesIO(base64.b64decode(encoded)))
    return None
