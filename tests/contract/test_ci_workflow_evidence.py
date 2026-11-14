"""Release evidence must belong to the merged PR, successful run and exact tree."""

import importlib.util
from pathlib import Path
from unittest.mock import patch


def load(name: str):
    path = Path(__file__).resolve().parents[2] / ".github" / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reuse_requires_exact_pr_run_and_tree():
    mod = load("ci_evidence")
    pr = {
        "number": 3,
        "merge_commit_sha": "merged",
        "merged_at": "now",
        "base": {"ref": "main", "sha": "base"},
        "head": {"sha": "head"},
    }
    run = {
        "id": 9,
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "head_sha": "head",
        "run_attempt": 1,
        "html_url": "https://example.test/run/9",
    }
    evidence = {
        "kind": "release-pr-full",
        "run_id": 9,
        "attempt": 1,
        "pr": 3,
        "base": "base",
        "head": "head",
        "tree": "tree",
    }
    with patch.object(mod, "command", return_value="tree"):
        assert (
            mod.matching_evidence("merged", [pr], [run], lambda _: evidence)
            == run["html_url"]
        )
        for key, value in [
            ("tree", "other"),
            ("base", "other"),
            ("head", "other"),
            ("pr", 4),
            ("run_id", 10),
            ("attempt", 2),
            ("kind", "docs"),
        ]:
            changed = {**evidence, key: value}
            assert (
                mod.matching_evidence(
                    "merged", [pr], [run], lambda _, item=changed: item
                )
                is None
            )
        assert (
            mod.matching_evidence(
                "merged", [pr], [{**run, "conclusion": "failure"}], lambda _: evidence
            )
            is None
        )
        assert (
            mod.matching_evidence("different", [pr], [run], lambda _: evidence) is None
        )
        assert mod.matching_evidence("merged", [pr], [run], lambda _: {}) is None


def test_doc_classification_is_conservative():
    mod = load("doc_check")
    assert mod.docs_only(["README.md", "docs/guide.md"])
    assert not mod.docs_only([])
    assert not mod.docs_only(["README.md", "pyproject.toml"])
    assert not mod.docs_only(["docs/guide.md", ".github/workflows/ci.yml"])


def test_test_selection_falls_back_for_mixed_or_missing_inputs(tmp_path, monkeypatch):
    mod = load("run_tests")
    monkeypatch.chdir(tmp_path)
    Path("tests").mkdir()
    Path("tests/test_example.py").write_text("", encoding="utf-8")
    assert mod.selection(["tests/test_example.py"]) == ["tests/test_example.py"]
    Path("tests/test_dependent.py").write_text(
        "from tests.test_example import helper\n", encoding="utf-8"
    )
    assert mod.selection(["tests/test_example.py"]) == []
    assert mod.selection(["tests/test_example.py", "src/a.py"]) == []
    assert mod.selection(["tests/conftest.py"]) == []
    assert mod.selection(["tests/test_missing.py"]) == []


def test_evidence_failures_never_reuse(tmp_path, monkeypatch):
    mod = load("ci_evidence")
    output = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_SHA", "unverified")
    monkeypatch.setattr(mod.sys, "argv", ["ci_evidence.py", "verify"])
    with patch.object(mod, "find_verified", side_effect=ValueError("unavailable")):
        mod.main()
    assert output.read_text(encoding="utf-8") == "reuse=false\n"


def test_docs_examples_and_link_failures(tmp_path, monkeypatch):
    import pytest

    mod = load("doc_check")
    monkeypatch.chdir(tmp_path)
    Path("ok.md").write_text("# OK\n", encoding="utf-8")
    Path("doc.md").write_text(
        '[ok](ok.md)\n```json\n{"ok":true}\n```\n', encoding="utf-8"
    )
    mod.check_doc("doc.md")
    Path("doc.md").write_text("[missing](missing.md)\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Broken local link"):
        mod.check_doc("doc.md")
    Path("doc.md").write_text("```json\n{invalid}\n```\n", encoding="utf-8")
    with pytest.raises(ValueError):
        mod.check_doc("doc.md")
