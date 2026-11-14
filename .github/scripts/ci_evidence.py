"""Associate a successful release-PR run with the exact merged Git tree."""

import json
import os
import pathlib
import subprocess
import sys
import tempfile


def command(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL).strip()


def api(path: str) -> object:
    return json.loads(command("gh", "api", path))


def matching_evidence(merge: str, pulls: list, runs: list, fetch) -> str | None:
    tree = command("git", "rev-parse", f"{merge}^{{tree}}")
    for pr in pulls:
        if (
            pr.get("merge_commit_sha") != merge
            or pr["base"]["ref"] != "main"
            or not pr.get("merged_at")
        ):
            continue
        for run in runs:
            if (
                run.get("event") != "pull_request"
                or run.get("status") != "completed"
                or run.get("conclusion") != "success"
                or run.get("head_sha") != pr["head"]["sha"]
            ):
                continue
            try:
                evidence = fetch(run["id"])
                if (
                    evidence["run_id"] == run["id"]
                    and evidence["pr"] == pr["number"]
                    and evidence["head"] == pr["head"]["sha"]
                    and evidence["base"] == pr["base"]["sha"]
                    and evidence["tree"] == tree
                    and evidence["attempt"] == run["run_attempt"]
                    and evidence["kind"] == "release-pr-full"
                ):
                    return run["html_url"]
            except (KeyError, OSError, ValueError, subprocess.SubprocessError):
                continue
    return None


def find_verified(merge: str) -> str | None:
    repo = os.environ["GITHUB_REPOSITORY"]
    pulls = api(f"repos/{repo}/commits/{merge}/pulls")
    workflows = api(f"repos/{repo}/actions/workflows/ci.yml")
    runs = api(
        f"repos/{repo}/actions/workflows/{workflows['id']}/runs?event=pull_request&per_page=100"
    )["workflow_runs"]
    runs = [
        run
        for run in runs
        if (run.get("head_repository") or {}).get("full_name") == repo
    ]
    pulls = [
        pr
        for pr in pulls
        if (pr.get("head", {}).get("repo") or {}).get("full_name") == repo
    ]

    def fetch(run_id: int) -> dict:
        with tempfile.TemporaryDirectory() as folder:
            command(
                "gh",
                "run",
                "download",
                str(run_id),
                "--repo",
                repo,
                "--name",
                "release-pr-evidence",
                "--dir",
                folder,
            )
            return json.loads(
                (pathlib.Path(folder) / "evidence.json").read_text(encoding="utf-8")
            )

    return matching_evidence(merge, pulls, runs, fetch)


def main() -> None:
    mode = sys.argv[1]
    if mode == "candidate":
        event = json.loads(
            pathlib.Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8")
        )
        pr = event["pull_request"]
        if pr["base"]["ref"] != "main" or pr["head"]["ref"] != "dev":
            raise ValueError("Not a dev to main release PR")
        evidence = {
            "kind": "release-pr-full",
            "run_id": int(os.environ["GITHUB_RUN_ID"]),
            "attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
            "pr": pr["number"],
            "head": pr["head"]["sha"],
            "base": pr["base"]["sha"],
            "tree": command("git", "rev-parse", "HEAD^{tree}"),
        }
        pathlib.Path("release-evidence").mkdir(exist_ok=True)
        pathlib.Path("release-evidence/evidence.json").write_text(
            json.dumps(evidence), encoding="utf-8"
        )
    elif mode == "verify":
        try:
            url = find_verified(os.environ["GITHUB_SHA"])
        except (KeyError, OSError, ValueError, subprocess.SubprocessError):
            url = None
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write(f"reuse={'true' if url else 'false'}\n")
            if url:
                out.write(f"candidate_url={url}\n")
        print(
            f"Matching release PR evidence: {url}"
            if url
            else "No matching evidence: full validation required"
        )
    else:
        raise ValueError("Unknown mode")


if __name__ == "__main__":
    main()
