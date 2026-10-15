"""Read the text in a photo with Tesseract (free, runs on your computer, no key)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from PIL import Image, ImageOps

try:  # iPhone photos are HEIC; this optional package lets Pillow open them.
    from pillow_heif import register_heif_opener  # type: ignore
    register_heif_opener()
except Exception:  # pragma: no cover - optional
    pass

try:
    import pytesseract  # type: ignore
except Exception:  # pragma: no cover
    pytesseract = None

_WINDOWS_DEFAULT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def _configure():
    if pytesseract is None:
        return False
    cmd = os.environ.get("KEPT_TESSERACT_CMD")
    if not cmd and not shutil.which("tesseract") and Path(_WINDOWS_DEFAULT).exists():
        cmd = _WINDOWS_DEFAULT
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    return True


def available() -> bool:
    if not _configure():
        return False
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def normalize_image(src: Path, dest: Path, max_side: int = 2000) -> Path:
    """Open any phone photo (JPEG, PNG, HEIC), fix its rotation, shrink it, save as JPEG."""
    img = Image.open(src)
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((max_side, max_side))
    img.save(dest, "JPEG", quality=88)
    return dest


def read_text(path: Path) -> tuple[str, bool]:
    """Returns (text, ok). ok is False when Tesseract is missing or fails."""
    if not available():
        return "", False
    try:
        img = Image.open(path)
        img = ImageOps.exif_transpose(img).convert("L")
        if max(img.size) < 1400:  # small screenshots read better a bit larger
            scale = 1400 / max(img.size)
            img = img.resize((int(img.width * scale), int(img.height * scale)))
        img = ImageOps.autocontrast(img)
        # psm 4 reads a page as rows, so a label and its value stay on one line
        return pytesseract.image_to_string(img, config="--psm 4"), True
    except Exception:
        return "", False
