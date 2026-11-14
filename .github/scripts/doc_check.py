"""Check changed Markdown examples, local links, and obvious credential material."""

import json
import pathlib
import re
import subprocess
import sys
import tomllib
from urllib.parse import unquote

import yaml

SECRET = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bAKIA[A-Z0-9]{16}\b"
)


def changed(base: str) -> list[str]:
    result = subprocess.check_output(["git", "diff", "--name-only", "-z", base, "HEAD"])
    return [p.decode("utf-8") for p in result.split(b"\0") if p]


def docs_only(paths: list[str]) -> bool:
    return bool(paths) and all(p.lower().endswith(".md") for p in paths)


def check_doc(name: str) -> None:
    path = pathlib.Path(name)
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    if SECRET.search(text):
        raise ValueError(f"Potential secret in {name}")
    for lang, content in re.findall(
        r"```(json|toml|yaml)\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE
    ):
        {"json": json.loads, "toml": tomllib.loads, "yaml": yaml.safe_load}[
            lang.lower()
        ](content)
    stripped = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    for link in re.findall(r"(?<!!)\[[^\]]+\]\(([^\s)]+)", stripped):
        dest = unquote(link.split("#", 1)[0])
        if not dest or re.match(r"^[a-zA-Z][\w+.-]*:", dest):
            continue
        if not (path.parent / dest).exists():
            raise ValueError(f"Broken local link in {name}: {link}")


def main() -> None:
    base = sys.argv[2]
    paths = changed(base)
    if sys.argv[1] == "classify":
        print("docs" if docs_only(paths) else "code")
    elif sys.argv[1] == "check":
        for path in paths:
            if path.lower().endswith(".md"):
                check_doc(path)
        print("Changed Markdown checks passed")
    elif sys.argv[1] == "secrets":
        diff = subprocess.check_output(
            ["git", "diff", "--unified=0", base, "HEAD"], text=True, encoding="utf-8"
        )
        for line in diff.splitlines():
            if (
                line.startswith("+")
                and not line.startswith("+++")
                and SECRET.search(line[1:])
            ):
                raise ValueError("Potential secret in added content")
        print("Added content secret scan passed")
    else:
        raise ValueError("Unknown mode")


if __name__ == "__main__":
    main()
