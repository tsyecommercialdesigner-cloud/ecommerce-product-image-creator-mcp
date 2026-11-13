import sys
from pathlib import Path
from typing import Self

import pytest

from ecommerce_product_image_creator import config

KEY = "ZHENZHEN_GPTIMAGEGEN_API_KEY"


def test_file_precedes_process_and_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(f'{KEY}="file-test"\n', encoding="utf-8")
    monkeypatch.setenv(KEY, "process-test")
    monkeypatch.setattr(config, "_read_windows_user_value", lambda _name: "user-test")
    assert config.load_config(tmp_path).api_key == "file-test"


def test_missing_file_falls_back_to_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KEY, "process-test")
    monkeypatch.setattr(config, "_read_windows_user_value", lambda _name: "user-test")
    assert config.load_config(tmp_path).api_key == "process-test"


def test_missing_file_and_process_fall_back_to_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(KEY, raising=False)
    monkeypatch.setattr(config, "_read_windows_user_value", lambda _name: "user-test")
    assert config.load_config(tmp_path).api_key == "user-test"


@pytest.mark.parametrize("text", [f"{KEY}=\n", f"{KEY}=  \n", f'{KEY}=""\n'])
def test_empty_file_value_does_not_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    (tmp_path / ".env").write_text(text, encoding="utf-8")
    monkeypatch.setenv(KEY, "process-test")
    with pytest.raises(config.ConfigError):
        config.load_config(tmp_path)


@pytest.mark.parametrize(
    "text", [f'{KEY}="unfinished\n', f"{KEY}=abc def\n", "BAD LINE\n"]
)
def test_invalid_file_is_rejected_and_redacted(tmp_path: Path, text: str) -> None:
    (tmp_path / ".env").write_text(text, encoding="utf-8")
    with pytest.raises(config.ConfigError) as caught:
        config.load_config(tmp_path)
    assert "unfinished" not in str(caught.value)
    assert "abc def" not in str(caught.value)


def test_duplicate_field_does_not_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(f"{KEY}=one\n{KEY}=two\n", encoding="utf-8")
    monkeypatch.setenv(KEY, "process-test")
    with pytest.raises(config.ConfigError):
        config.load_config(tmp_path)


def test_invalid_encoding_fails_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_bytes(b"\xff")
    monkeypatch.setenv(KEY, "process-test")
    with pytest.raises(config.ConfigError):
        config.load_config(tmp_path)


def test_unreadable_path_fails_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").mkdir()
    monkeypatch.setenv(KEY, "process-test")
    with pytest.raises(config.ConfigError):
        config.load_config(tmp_path)


def test_empty_process_value_does_not_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KEY, "")
    monkeypatch.setattr(config, "_read_windows_user_value", lambda _name: "user-test")
    with pytest.raises(config.ConfigError):
        config.load_config(tmp_path)


def test_missing_all_sources_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(KEY, raising=False)
    monkeypatch.setattr(config, "_read_windows_user_value", lambda _name: None)
    with pytest.raises(config.ConfigError):
        config.load_config(tmp_path)


def test_user_source_error_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(KEY, raising=False)

    def fail(_name: str) -> str | None:
        raise OSError("sensitive-test-value")

    monkeypatch.setattr(config, "_read_windows_user_value", fail)
    with pytest.raises(config.ConfigError) as caught:
        config.load_config(tmp_path)
    assert "sensitive-test-value" not in str(caught.value)


def test_repr_does_not_expose_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KEY, "sensitive-test-value")
    assert "sensitive-test-value" not in repr(config.load_config(tmp_path))


def test_default_root_is_independent_of_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert config.project_root() == Path(config.__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry integration")
def test_windows_user_environment_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    import winreg

    class FakeKey:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

    monkeypatch.setattr(winreg, "OpenKey", lambda *_args: FakeKey())
    monkeypatch.setattr(
        winreg, "QueryValueEx", lambda *_args: ("registry-test", winreg.REG_SZ)
    )
    assert config._read_windows_user_value(KEY) == "registry-test"


@pytest.mark.parametrize("value", ["", "  ", "two words", "contains\nnewline"])
def test_invalid_user_value_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.delenv(KEY, raising=False)
    monkeypatch.setattr(config, "_read_windows_user_value", lambda _name: value)
    with pytest.raises(config.ConfigError):
        config.load_config(tmp_path)
