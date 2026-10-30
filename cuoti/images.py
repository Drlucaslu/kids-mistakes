"""Photo storage: upright, shrunk to 1600px, saved as JPEG under DATA_DIR/photos/<yyyy-mm>/."""
from __future__ import annotations

import io
import secrets
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from . import db
from .config import get_settings

MAX_SIDE = 1600
MAX_BYTES = 20 * 1024 * 1024
MAX_PHOTOS = 6


class PhotoError(ValueError):
    pass


def check(data: bytes) -> None:
    """Raise PhotoError (with a message for the person) unless data is an image we can store."""
    from .i18n import t
    if len(data) > MAX_BYTES:
        raise PhotoError(t("照片太大（最大 {n} MB）", n=MAX_BYTES // (1024 * 1024)))
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
    except (UnidentifiedImageError, OSError, SyntaxError) as e:
        raise PhotoError(t("有一张照片无法识别，请换一张（JPG/PNG）")) from e


def check_all(datas: list[bytes]) -> None:
    from .i18n import t
    if len(datas) > MAX_PHOTOS:
        raise PhotoError(t("一次最多 {n} 张照片", n=MAX_PHOTOS))
    for d in datas:
        check(d)


def save_photo(data: bytes) -> str:
    """Returns the path relative to the photo dir. Call check() first."""
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    sub = db.now().strftime("%Y-%m")
    rel = f"{sub}/{secrets.token_hex(8)}.jpg"
    path = get_settings().photo_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=85, optimize=True)
    return rel


def photo_path(rel: str) -> Path:
    base = get_settings().photo_dir.resolve()
    p = (base / rel).resolve()
    if base not in p.parents:
        raise ValueError("bad photo path")
    return p
