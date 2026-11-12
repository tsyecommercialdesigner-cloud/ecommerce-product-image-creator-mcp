import io
import os
from pathlib import Path

import pytest
from PIL import Image

from ecommerce_product_image_creator import storage
from ecommerce_product_image_creator.storage import (
    FileSafetyError,
    ResourceBudget,
    read_reference,
)


def image_bytes(fmt: str) -> bytes:
    data = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(data, format=fmt)
    return data.getvalue()


@pytest.fixture
def budget() -> ResourceBudget:
    return ResourceBudget(
        max_file_bytes=100_000, max_pixels=1_000, max_decoded_bytes=10_000
    )


@pytest.mark.parametrize("fmt,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg")])
def test_image_bytes_and_digest(
    tmp_path: Path, budget: ResourceBudget, fmt: str, mime: str
) -> None:
    import hashlib

    image = tmp_path / "product.img"
    raw = image_bytes(fmt)
    image.write_bytes(raw)
    snapshot = read_reference(tmp_path, image, mime, budget)
    assert snapshot.data == raw
    assert snapshot.sha256 == hashlib.sha256(raw).hexdigest()
    assert snapshot.mime == mime


def test_rejects_outside_root_and_invalid_data(
    tmp_path: Path, budget: ResourceBudget
) -> None:
    root = tmp_path / "input"
    root.mkdir()
    other = tmp_path / "other.png"
    other.write_bytes(image_bytes("PNG"))
    with pytest.raises(FileSafetyError):
        read_reference(root, other, "image/png", budget)
    corrupt = root / "bad.png"
    corrupt.write_bytes(image_bytes("PNG")[:20])
    with pytest.raises(FileSafetyError):
        read_reference(root, corrupt, "image/png", budget)
    valid = root / "valid.png"
    valid.write_bytes(image_bytes("PNG"))
    with pytest.raises(FileSafetyError):
        read_reference(root, valid, "image/jpeg", budget)
    with pytest.raises(FileSafetyError):
        read_reference(root, root / "missing", "image/png", budget)


def test_resource_budgets_are_caller_supplied(tmp_path: Path) -> None:
    image = tmp_path / "product.png"
    image.write_bytes(image_bytes("PNG"))
    for budget in (
        ResourceBudget(20, 1000, 10000),
        ResourceBudget(10000, 10, 10000),
        ResourceBudget(10000, 1000, 20),
    ):
        with pytest.raises(FileSafetyError):
            read_reference(tmp_path, image, "image/png", budget)


def test_detects_change_during_read(
    tmp_path: Path, budget: ResourceBudget, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "product.png"
    image.write_bytes(image_bytes("PNG"))
    original = os.fstat
    count = 0

    def modified(fd: int) -> os.stat_result:
        nonlocal count
        result = original(fd)
        count += 1
        if count == 1:
            with image.open("ab") as stream:
                stream.write(b"changed")
        return result

    monkeypatch.setattr(storage.os, "fstat", modified)
    with pytest.raises(FileSafetyError):
        read_reference(tmp_path, image, "image/png", budget)


def test_unreadable_input_does_not_expose_path(
    tmp_path: Path, budget: ResourceBudget, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "sensitive-name.png"
    image.write_bytes(image_bytes("PNG"))

    def denied(*_args: object, **_kwargs: object) -> int:
        raise PermissionError("sensitive-name.png")

    monkeypatch.setattr(storage.os, "open", denied)
    with pytest.raises(FileSafetyError) as caught:
        read_reference(tmp_path, image, "image/png", budget)
    assert "sensitive-name.png" not in str(caught.value)


def test_memory_exhaustion_is_reported_as_safe_failure(
    tmp_path: Path, budget: ResourceBudget, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "product.png"
    image.write_bytes(image_bytes("PNG"))

    def exhausted(*_args: object, **_kwargs: object) -> None:
        raise MemoryError("private diagnostic")

    monkeypatch.setattr(storage.Image, "open", exhausted)
    with pytest.raises(FileSafetyError) as caught:
        read_reference(tmp_path, image, "image/png", budget)
    assert "private diagnostic" not in str(caught.value)


def test_reparse_point_rejected(tmp_path: Path, budget: ResourceBudget) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(image_bytes("PNG"))
    link = root / "link.png"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Current Windows account cannot create symbolic links")
    with pytest.raises(FileSafetyError):
        read_reference(root, link, "image/png", budget)

    root_link = tmp_path / "root-link"
    root_link.symlink_to(root, target_is_directory=True)
    with pytest.raises(FileSafetyError):
        read_reference(root_link, root_link / "link.png", "image/png", budget)
