"""Use a narrow test selection only when all changed files are tests."""

import os
import subprocess
import sys
from pathlib import Path


def selection(paths: list[str]) -> list[str]:
    if paths and all(p.startswith("tests/") and p.endswith(".py") for p in paths):
        existing = [
            p
            for p in paths
            if os.path.isfile(p) and os.path.basename(p).startswith("test_")
        ]
        if len(existing) == len(paths):
            # Some tests supply fixtures to other tests. In that case a narrow
            # run is unsafe; shared files and ambiguous reads also fail closed.
            for item in existing:
                module = item[:-3].replace("/", ".")
                stem = Path(item).stem
                for other in Path("tests").rglob("*.py"):
                    if other.as_posix() == item:
                        continue
                    try:
                        text = other.read_text(encoding="utf-8")
                    except (OSError, UnicodeError):
                        return []
                    if module in text or stem in text:
                        return []
            return existing
    return []


def main() -> None:
    base = os.environ.get("TEST_SELECTION_BASE", "")
    selected: list[str] = []
    if base:
        raw = subprocess.check_output(
            ["git", "diff", "--name-only", "-z", base, "HEAD"]
        )
        selected = selection([p.decode("utf-8") for p in raw.split(b"\0") if p])
    print(
        "Selected tests: " + (", ".join(selected) if selected else "full suite"),
        flush=True,
    )
    args = [sys.executable, "-m", "pytest", "-q", "--basetemp", sys.argv[1], *selected]
    raise SystemExit(subprocess.call(args))


if __name__ == "__main__":
    main()
