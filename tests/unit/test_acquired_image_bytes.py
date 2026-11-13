import io

import pytest
from PIL import Image

from ecommerce_product_image_creator.storage import (
    FileSafetyError,
    ResourceBudget,
    validate_image_bytes,
)


def image_bytes(kind):
    stream = io.BytesIO()
    Image.new("RGB", (5, 5), "blue").save(stream, kind)
    return stream.getvalue()


@pytest.mark.parametrize("kind,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg")])
def test_shared_image_byte_validation(kind, mime):
    data = image_bytes(kind)
    result = validate_image_bytes(data, mime, ResourceBudget(100000, 100, 1000))
    assert (result.mime, result.width, result.height) == (mime, 5, 5)
    with pytest.raises(FileSafetyError):
        validate_image_bytes(data[:-10], mime, ResourceBudget(100000, 100, 1000))
    with pytest.raises(FileSafetyError):
        validate_image_bytes(data, "image/gif", ResourceBudget(100000, 100, 1000))
    with pytest.raises(FileSafetyError):
        validate_image_bytes(data, mime, ResourceBudget(1, 100, 1000))
    with pytest.raises(FileSafetyError):
        validate_image_bytes(data, mime, ResourceBudget(100000, 10, 1000))
    with pytest.raises(FileSafetyError):
        validate_image_bytes(data, mime, ResourceBudget(100000, 100, 10))


def test_windows_stage_directory_pin_blocks_rename(tmp_path):
    import sys

    if sys.platform != "win32":
        pytest.skip("Windows file handle semantics")
    from ecommerce_product_image_creator.recovery_intent import _pin_directories

    root = tmp_path / "pinned"
    root.mkdir()
    with _pin_directories(root), pytest.raises(OSError):
        root.rename(tmp_path / "moved")
    assert root.is_dir()


def test_windows_stage_directory_pin_rejects_symlink(tmp_path):
    import os
    import sys

    if sys.platform != "win32":
        pytest.skip("Windows file handle semantics")
    from ecommerce_product_image_creator.recovery_intent import _pin_directories

    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    os.symlink(target, link, target_is_directory=True)
    with (
        pytest.raises(ValueError, match="stage_directory_unverified"),
        _pin_directories(link),
    ):
        pass


def test_windows_stage_directory_pin_rejects_junction(tmp_path):
    import os
    import subprocess
    import sys

    if sys.platform != "win32":
        pytest.skip("Windows file handle semantics")
    from ecommerce_product_image_creator.recovery_intent import _pin_directories

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "junction"
    environment = dict(
        os.environ, T038_JUNCTION_PATH=str(link), T038_JUNCTION_TARGET=str(target)
    )
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "New-Item -ItemType Junction -Path $env:T038_JUNCTION_PATH -Target $env:T038_JUNCTION_TARGET | Out-Null",
        ],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, "junction creation unavailable"
    with (
        pytest.raises(ValueError, match="stage_directory_unverified"),
        _pin_directories(link),
    ):
        pass


def test_ntfs_hardlink_is_no_overwrite(tmp_path):
    import os
    import sys

    if sys.platform != "win32":
        pytest.skip("Windows file handle semantics")
    from ecommerce_product_image_creator.recovery_intent import _pin_directories

    staged = tmp_path / "stage"
    target = tmp_path / "target"
    staged.write_bytes(b"first")
    with _pin_directories(tmp_path):
        os.link(staged, target)
        assert target.read_bytes() == b"first"
        staged.write_bytes(b"second")
        assert target.read_bytes() == b"second"
        with pytest.raises(FileExistsError):
            os.link(staged, target)
        assert target.read_bytes() == b"second"


def test_windows_pinned_file_blocks_writes_while_linking(tmp_path):
    import os
    import sys

    if sys.platform != "win32":
        pytest.skip("Windows file handle semantics")
    from ecommerce_product_image_creator.recovery_intent import (
        _pin_directories,
        _pin_file,
    )

    staged = tmp_path / "stage"
    target = tmp_path / "target"
    staged.write_bytes(b"immutable while open")
    with _pin_directories(tmp_path), _pin_file(staged):
        with pytest.raises(OSError):
            staged.write_bytes(b"change")
        os.link(staged, target)
        assert target.read_bytes() == b"immutable while open"
        with pytest.raises(OSError):
            target.write_bytes(b"change")
    assert target.read_bytes() == b"immutable while open"
