"""Small cached MCP previews derived only from verified registered artifacts."""

import base64
import hashlib
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mcp import types
from PIL import Image, ImageOps

from .storage import ResourceBudget, read_reference


def original_image(
    view: dict[str, Any], state: Path, budget: ResourceBudget
) -> types.ImageContent:
    """Return only a verified, registered delivered original as MCP image content."""
    file = view.get("file", {})
    if (
        view.get("delivery_status") != "delivered"
        or file.get("state") != "verified"
        or not file.get("path")
        or not file.get("mime")
        or not view.get("digest")
    ):
        raise ValueError("artifact_not_available")
    image = read_reference(state, Path(file["path"]), file["mime"], budget)
    if (
        image.sha256 != view["digest"]
        or hashlib.sha256(image.data).hexdigest() != view["digest"]
    ):
        raise ValueError("artifact_identity_changed")
    return types.ImageContent(
        type="image",
        data=base64.b64encode(image.data).decode("ascii"),
        mime_type=image.mime,
    )


@lru_cache(maxsize=4)
def _thumbnail(data: bytes) -> tuple[bytes, str]:
    with Image.open(BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source)
        image.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        buffer = BytesIO()
        if image.mode in ("RGBA", "LA"):
            image.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue(), "image/png"
        image.convert("RGB").save(buffer, format="JPEG", quality=85, optimize=True)
        return buffer.getvalue(), "image/jpeg"


def preview(
    view: dict[str, Any], state: Path, budget: ResourceBudget
) -> tuple[types.ImageContent, str] | None:
    """Select the newest delivered artifact and revalidate before cache access."""
    candidates = []
    if "artifact_id" in view:
        candidates.append(view)
    else:
        for attempt in reversed(view.get("attempts", [])):
            if attempt.get("delivery_status") == "delivered":
                candidates.extend(reversed(attempt.get("artifacts", [])))
    for artifact in candidates:
        file = artifact.get("file", {})
        if (
            artifact.get("delivery_status") != "delivered"
            or file.get("state") != "verified"
            or not file.get("path")
            or not file.get("mime")
            or not artifact.get("digest")
        ):
            continue
        original = read_reference(state, Path(file["path"]), file["mime"], budget)
        if (
            original.sha256 != artifact["digest"]
            or hashlib.sha256(original.data).hexdigest() != artifact["digest"]
        ):
            raise ValueError("artifact_preview_identity_changed")
        data, mime = _thumbnail(original.data)
        content = types.ImageContent(
            type="image", data=base64.b64encode(data).decode("ascii"), mime_type=mime
        )
        # Use only the verified registered path; escape Markdown delimiters and whitespace.
        path = quote(Path(file["path"]).as_posix(), safe="/:")
        return content, f"![商品图]({path})"
    return None
