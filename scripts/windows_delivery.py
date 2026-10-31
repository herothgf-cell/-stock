"""Seal tested Windows artifacts and publish only the EXE and its manifest.

Publishing uses an ordinary fast-forward push. It never rebases a build onto
newer source, force-pushes, or stages unrelated files.
"""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


EXE_NAME = "StockJournal.exe"
MANIFEST_NAME = "StockJournal-build.json"
DELIVERY_FILES = (EXE_NAME, MANIFEST_NAME)
PAGES = {"dashboard", "trades", "imports", "dividends", "instruments", "settings", "strategy"}
# Leave room below GitHub's 100 MiB per-file hard limit.
MAX_EXE_BYTES = 95 * 1024 * 1024


def git(repo, *args, check=True):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True,
    )
    if check and result.returncode:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result


def source_version(repo):
    """Read the literal version without importing application code in the publisher."""
    tree = ast.parse((Path(repo) / "stockjournal" / "__init__.py").read_text(encoding="utf-8"))
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        ):
            version = ast.literal_eval(statement.value)
            if isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version):
                return version
    raise ValueError("The source must define a literal semantic __version__")


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_smoke_report(path, *, frozen):
    report = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(report, dict) or report.get("ok") is not True or report.get("closed") is not True:
        raise ValueError(f"GUI verification failed or did not close cleanly: {path}")
    if report.get("frozen") is not frozen:
        raise ValueError(f"GUI report has the wrong frozen state: {path}")
    pages = report.get("pages")
    if not isinstance(pages, list) or len(pages) != len(PAGES) or set(pages) != PAGES:
        raise ValueError(f"GUI verification did not visit all {len(PAGES)} pages: {path}")
    return report


def _validate_exe(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Delivery executable must be a regular file")
    size = path.stat().st_size
    if not 0 < size < MAX_EXE_BYTES:
        raise ValueError("Delivery executable size exceeds the repository limit or is empty")
    with path.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("Delivery file is not a Windows executable")
    return size


def _validate_source(repo, source_commit, *, include_untracked):
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("Expected a full lowercase source commit SHA")
    if git(repo, "rev-parse", "HEAD").stdout.strip() != source_commit:
        raise ValueError("Checkout HEAD does not match the built source commit")
    flag = "--untracked-files=all" if include_untracked else "--untracked-files=no"
    if git(repo, "status", "--porcelain", flag).stdout.strip():
        raise ValueError("A clean source checkout is required; commit source edits first")


def seal_delivery(repo, source_commit, source_report, exe_report):
    """Create the manifest only after both real processes have passed their checks."""
    repo = Path(repo)
    _validate_source(repo, source_commit, include_untracked=False)
    validate_smoke_report(source_report, frozen=False)
    validate_smoke_report(exe_report, frozen=True)
    exe = repo / "dist" / EXE_NAME
    size = _validate_exe(exe)
    manifest = {
        "schema_version": 1,
        "version": source_version(repo),
        "source_commit": source_commit,
        "built_at_utc": datetime.fromtimestamp(exe.stat().st_mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "platform": "windows-x64",
        "filename": EXE_NAME,
        "sha256": sha256(exe),
        "size_bytes": size,
        "verification": {
            "source_gui": True,
            "frozen_gui": True,
            "source_report_sha256": sha256(source_report),
            "frozen_report_sha256": sha256(exe_report),
        },
    }
    (repo / "dist" / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def validate_delivery(repo, delivery_dir, source_commit):
    delivery_dir = Path(delivery_dir)
    manifest_path = delivery_dir / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > 65536:
        raise ValueError("Delivery manifest is missing or invalid")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("Unsupported delivery manifest")
    if manifest.get("source_commit") != source_commit:
        raise ValueError("Artifact source commit does not match this workflow's source")
    if manifest.get("version") != source_version(repo):
        raise ValueError("Artifact version does not match its source")
    verification = manifest.get("verification")
    if not isinstance(verification, dict) or any(verification.get(key) is not True for key in ("source_gui", "frozen_gui")):
        raise ValueError("Artifact has not passed source and frozen GUI verification")
    if manifest.get("filename") != EXE_NAME or manifest.get("platform") != "windows-x64":
        raise ValueError("Unexpected artifact filename or platform")
    timestamp = manifest.get("built_at_utc")
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise ValueError("Artifact build time must be in UTC")
    datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    exe = delivery_dir / EXE_NAME
    size = _validate_exe(exe)
    if type(manifest.get("size_bytes")) is not int or manifest["size_bytes"] != size:
        raise ValueError("Artifact size does not match its manifest")
    if manifest.get("sha256") != sha256(exe):
        raise ValueError("Artifact SHA-256 does not match its manifest")
    return manifest


def remote_main(repo):
    git(repo, "fetch", "--no-tags", "origin", "refs/heads/main")
    return git(repo, "rev-parse", "FETCH_HEAD").stdout.strip()


def publish_delivery(repo, delivery_dir, source_commit):
    """Publish only when main still names the exact source that was built.

    The initial fetch rejects builds already stale. A non-force push provides
    the second guard if main moves between that fetch and publication.
    """
    repo, delivery_dir = Path(repo), Path(delivery_dir)
    _validate_source(repo, source_commit, include_untracked=True)
    manifest = validate_delivery(repo, delivery_dir, source_commit)
    result = {"status": "stale", "version": manifest["version"], "source_commit": source_commit, "commit": ""}
    if remote_main(repo) != source_commit:
        return result
    for name in DELIVERY_FILES:
        destination = repo / name
        if destination.is_symlink():
            raise ValueError("Refusing to overwrite a delivery symlink")
        shutil.copyfile(delivery_dir / name, destination)
    git(repo, "add", "--", *DELIVERY_FILES)
    staged = set(git(repo, "diff", "--cached", "--name-only").stdout.splitlines())
    if not staged or not staged.issubset(DELIVERY_FILES):
        raise ValueError("Publication must change only the executable and its manifest")
    git(
        repo, "-c", "user.name=github-actions[bot]",
        "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit", "-m", f"Publish verified Windows executable v{manifest['version']} [skip ci]",
        "--", *DELIVERY_FILES,
    )
    commit = git(repo, "rev-parse", "HEAD").stdout.strip()
    pushed = git(repo, "push", "origin", "HEAD:refs/heads/main", check=False)
    if pushed.returncode:
        current = remote_main(repo)
        if current == commit:
            # The server may have accepted the push before the connection ended.
            pass
        elif current != source_commit:
            return result
        else:
            raise RuntimeError("Publication push failed; main was left unchanged: " + pushed.stderr.strip())
    return {**result, "status": "published", "commit": commit}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--delivery-dir", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    result = publish_delivery(args.repo, args.delivery_dir, args.source_commit)
    print(json.dumps(result, indent=2))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as stream:
            for key, value in result.items():
                stream.write(f"{key}={value}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as stream:
            if result["status"] == "published":
                stream.write(f"## Clone-and-run EXE published\n\nVersion **{result['version']}**, source `{result['source_commit']}`, delivery commit `{result['commit']}`.\n")
            else:
                stream.write("## Older build skipped\n\n`main` advanced while this build was running. No remote files were overwritten; the latest source build is responsible for publishing its EXE.\n")


if __name__ == "__main__":
    main()
