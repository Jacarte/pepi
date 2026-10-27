"""SWE-bench Multilingual task repos: import dataset rows, list task images, validate gold patches.

Usage: python -m evals.swebench.multilingual {import,images,validate-gold} --help

`import` writes the task-repo layout `swebench.task.repo.load_task_repo` reads (sweb.yaml plus
tasks/<id>/{task.yaml,tests.json,problem_statement.md,hints.md,gold.patch,test.patch,eval.sh,Dockerfile})
and an import-manifest.json with per-file hashes. Only `import --dataset` downloads (HuggingFace);
`validate-gold` runs the official grader through grade_driver.py and needs Docker plus the task images.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from .common import identifier, read_json, sha256_file, write_json
from .preset import task_input_fingerprints

DATASET_NAME = "SWE-bench/SWE-bench_Multilingual"
SPLIT = "test"
IMPORTER_VERSION = 1
MANIFEST = "import-manifest.json"
SWEB_YAML = "sweb.yaml"
ID_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
DATASET_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+")
# task.yaml scalars: restrictive ASCII so the hand-written YAML below is unambiguous.
SCALAR_PATTERNS = {
    "base_commit": re.compile(r"[0-9a-f]{40}"),
    "created_at": re.compile(r"[0-9A-Za-z :.+-]{1,40}"),
    "eval_type": re.compile(r"pass_and_fail|fail_only"),
    "image": re.compile(r"[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9._-]+)?(?:@sha256:[0-9a-f]{64})?"),
    "log_parser": re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}"),
    "repo": re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+"),
    "version": re.compile(r"[A-Za-z0-9._-]{1,64}"),
}
# Dataset columns kept as their own file (same names as swebench.task.repo.AS_FILE).
TEXT_FILES = {"patch": "gold.patch", "test_patch": "test.patch", "eval_script": "eval.sh",
              "problem_statement": "problem_statement.md"}
HINTS_FILE = "hints.md"
TESTS_FILE = "tests.json"
TASK_FILE = "task.yaml"
DOCKERFILE = "Dockerfile"


def parse_ids(value: str) -> list[str]:
    """A file of IDs (one per line, # comments) or a comma-separated list; ordered, unique."""
    path = Path(value)
    lines = path.read_text(encoding="utf-8").split("\n") if path.is_file() else value.split(",")
    ids = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    for instance_id in ids:
        if not ID_PATTERN.fullmatch(instance_id):
            raise ValueError(f"Unsafe instance ID: {instance_id!r}")
        identifier(instance_id)
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("Select at least one task; duplicate IDs are not allowed")
    return ids


def canonical_sha256(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def test_list(row: dict, key: str) -> list[str]:
    value = row.get(key)
    if isinstance(value, str):  # original SWE-bench ships these as JSON strings
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{row.get('instance_id')}: {key} must be a list of strings")
    return value


def validate_row(row: dict) -> dict:
    """The fields a task directory is written from; refuses anything incomplete or unsafe."""
    if not isinstance(row, dict):
        raise ValueError("Each dataset row must be an object")
    instance_id = row.get("instance_id")
    if not isinstance(instance_id, str) or not ID_PATTERN.fullmatch(instance_id):
        raise ValueError(f"Unsafe or missing instance_id: {instance_id!r}")
    identifier(instance_id)
    task = {"instance_id": instance_id}
    for key, pattern in SCALAR_PATTERNS.items():
        value = row.get(key)
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise ValueError(f"{instance_id}: missing or invalid {key}")
        task[key] = value
    for key in TEXT_FILES:
        value = row.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{instance_id}: missing {key}")
        task[key] = value
    hints = row.get("hints_text")
    if hints is not None and not isinstance(hints, str):
        raise ValueError(f"{instance_id}: hints_text must be a string")
    task["hints_text"] = hints or ""
    task["tests"] = {"FAIL_TO_PASS": test_list(row, "FAIL_TO_PASS"),
                     "PASS_TO_PASS": test_list(row, "PASS_TO_PASS")}
    if not task["tests"]["FAIL_TO_PASS"]:
        raise ValueError(f"{instance_id}: FAIL_TO_PASS is empty")
    return task


def task_yaml(task: dict, dataset: str) -> str:
    """Sorted block mapping of double-quoted ASCII scalars (YAML-safe JSON strings)."""
    fields = {key: task[key] for key in SCALAR_PATTERNS} | {
        "instance_id": task["instance_id"], "split": SPLIT}
    lines = []
    for key in sorted(fields | {"datasets": None}):
        if key == "datasets":
            lines += ["datasets:", f"- {json.dumps(dataset)}"]
        else:
            lines.append(f"{key}: {json.dumps(fields[key])}")
    return "\n".join(lines) + "\n"


def sweb_yaml(dataset: str) -> str:
    return f"datasets:\n- {json.dumps(dataset)}\nsplits:\n- {json.dumps(SPLIT)}\n"


def task_files(task: dict, dataset: str) -> dict[str, str]:
    files = {name: task[column] for column, name in TEXT_FILES.items()}
    if task["hints_text"]:
        files[HINTS_FILE] = task["hints_text"]
    files[TESTS_FILE] = json.dumps(task["tests"], indent=2, ensure_ascii=False) + "\n"
    files[TASK_FILE] = task_yaml(task, dataset)
    # The harness builds every task-repo image before grading and fails without a Dockerfile;
    # the published prebuilt image is the whole environment, so the build only re-tags it.
    files[DOCKERFILE] = f"FROM --platform=linux/amd64 {task['image']}\n"
    return files


def select_rows(rows: list, ids: list[str]) -> list[dict]:
    by_id: dict[str, dict] = {}
    for row in rows:
        instance_id = row.get("instance_id") if isinstance(row, dict) else None
        if instance_id in ids:
            if instance_id in by_id:
                raise ValueError(f"Duplicate dataset row: {instance_id}")
            by_id[instance_id] = row
    missing = [instance_id for instance_id in ids if instance_id not in by_id]
    if missing:
        raise ValueError(f"Not in the dataset rows: {' '.join(missing)}")
    return [by_id[instance_id] for instance_id in ids]


def write_task_repo(out: Path, rows: list, ids: list[str], dataset: str, source: dict) -> dict:
    """Write the task repo atomically (temporary sibling directory, then rename); never overwrite."""
    if not DATASET_PATTERN.fullmatch(dataset):
        raise ValueError(f"Invalid dataset name: {dataset!r}")
    selected = select_rows(rows, ids)
    tasks = [validate_row(row) for row in selected]
    out = out.absolute()
    if out.exists() or out.is_symlink():
        raise ValueError(f"Refusing to overwrite an existing path: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{out.name}.", dir=out.parent))
    try:
        os.chmod(staging, 0o755)
        (staging / SWEB_YAML).write_bytes(sweb_yaml(dataset).encode("utf-8"))
        manifest_tasks = {}
        for row, task in zip(selected, tasks):
            directory = staging / "tasks" / task["instance_id"]
            directory.mkdir(parents=True)
            hashes = {}
            for name, text in task_files(task, dataset).items():
                data = text.encode("utf-8")  # bytes as given: no newline translation
                (directory / name).write_bytes(data)
                hashes[name] = hashlib.sha256(data).hexdigest()
            manifest_tasks[task["instance_id"]] = {
                "repo": task["repo"], "base_commit": task["base_commit"], "image": task["image"],
                "row_sha256": canonical_sha256(row), "files": dict(sorted(hashes.items())),
                "fingerprint": task_input_fingerprints(staging, [task["instance_id"]])[task["instance_id"]]}
        manifest = {"schema_version": 1, "importer_version": IMPORTER_VERSION, "dataset": dataset,
                    "split": SPLIT, "source": source, "instance_ids": ids,
                    "rows_sha256": canonical_sha256(selected),
                    "sweb_yaml_sha256": sha256_file(staging / SWEB_YAML), "tasks": manifest_tasks}
        write_json(staging / MANIFEST, manifest)
        if out.exists() or out.is_symlink():
            raise ValueError(f"Refusing to overwrite an existing path: {out}")
        os.rename(staging, out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def load_dataset_rows(name: str, revision: str | None) -> tuple[list, str]:
    """Download one pinned dataset revision; imported lazily so the module works without `datasets`."""
    try:
        from datasets import load_dataset
        from huggingface_hub import HfApi
    except ImportError as error:
        raise ImportError(f"{error}; run with the swebench venv (~/.venvs/swebench/bin/python)") from None
    resolved = HfApi().dataset_info(name, revision=revision).sha
    if not isinstance(resolved, str) or not re.fullmatch(r"[0-9a-f]{40}", resolved):
        raise ValueError(f"Could not resolve a dataset commit for {name}")
    return [dict(row) for row in load_dataset(name, split=SPLIT, revision=resolved)], resolved


def verified_manifest(task_repo: Path, ids: list[str] | None = None) -> dict:
    """The import manifest, after checking the selected task files still have their imported bytes."""
    manifest = read_json(task_repo / MANIFEST)
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("tasks"), dict):
        raise ValueError(f"Unsupported or missing {MANIFEST} in {task_repo}")
    if sha256_file(task_repo / SWEB_YAML) != manifest["sweb_yaml_sha256"]:
        raise ValueError(f"{SWEB_YAML} changed since import; re-import")
    ids = manifest["instance_ids"] if ids is None else ids
    changed = []
    for instance_id in ids:
        entry = manifest["tasks"].get(instance_id)
        if entry is None:
            raise ValueError(f"Not imported in {task_repo}: {instance_id}")
        directory = task_repo / "tasks" / identifier(instance_id)
        present = sorted(path.name for path in directory.iterdir()) if directory.is_dir() else []
        if present != sorted(entry["files"]) or any(
                (directory / name).is_symlink() or sha256_file(directory / name) != digest
                for name, digest in entry["files"].items()):
            changed.append(instance_id)
    if changed:
        raise ValueError(f"Task files changed since import: {' '.join(changed)}; re-import")
    return manifest


def report_status(path: Path, instance_id: str) -> str:
    try:
        report = read_json(path)[instance_id]
    except (OSError, ValueError, KeyError, TypeError):
        return "missing"
    return "resolved" if isinstance(report, dict) and report.get("resolved") is True else "unresolved"


def validate_gold(task_repo: Path, ids: list[str], run_id: str, workers: int, timeout: int,
                  work: Path) -> int:
    """Grade the gold patches exactly as `reporting grade` grades predictions: grade_driver.py
    over a frozen copy of the task repo. Exit 0 only when every task resolves."""
    from .reporting import freeze_task_repo, run_grader
    if workers <= 0 or timeout <= 0:
        raise ValueError("Workers and grading timeout must be positive")
    identifier(run_id)
    task_repo = task_repo.absolute()
    manifest = verified_manifest(task_repo, ids)
    work = work.absolute()
    work.mkdir(parents=True, exist_ok=False)
    expected = []
    for instance_id in ids:
        entry = manifest["tasks"][instance_id]
        statement = (task_repo / "tasks" / instance_id / "problem_statement.md").read_bytes().decode("utf-8")
        expected.append({"instance_id": instance_id, "repo": entry["repo"], "base_commit": entry["base_commit"],
                         "image": entry["image"], "problem_statement": statement})
    frozen = work / "task-repo"
    grader_sha256 = freeze_task_repo(task_repo, frozen, expected)
    write_json(work / "arguments.json", {
        "dataset_name": manifest["dataset"], "split": manifest["split"], "instance_ids": ids,
        "predictions_path": "gold", "max_workers": workers, "open_file_limit": 4096, "run_id": run_id,
        "timeout": timeout, "rewrite_reports": False, "modal": False, "task_repo": str(frozen),
        "expected_tasks": expected})
    record = {"schema_version": 1, "run_id": run_id, "dataset": manifest["dataset"], "instance_ids": ids,
              "grader_sha256": grader_sha256, "workers": workers, "test_timeout_seconds": timeout,
              "exit_code": None}
    code = run_grader(work, record, [work / "validate-gold.json"])
    reports = work / "logs/evaluation" / run_id / "gold"
    results = {instance_id: report_status(reports / instance_id / "report.json", instance_id)
               for instance_id in ids}
    write_json(work / "validate-gold.json", record | {"results": results})
    width = max(len(instance_id) for instance_id in ids)
    for instance_id in ids:
        print(f"{instance_id:<{width}}  {results[instance_id]}")
    resolved = sum(status == "resolved" for status in results.values())
    print(f"{resolved}/{len(ids)} gold patches resolved; details in {work}")
    return 0 if code == 0 and resolved == len(ids) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals.swebench.multilingual", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import", help="write a task repo from dataset rows")
    source = importer.add_mutually_exclusive_group(required=True)
    source.add_argument("--rows-json", type=Path, help="local JSON list of dataset rows (no download)")
    source.add_argument("--dataset", help=f"HuggingFace dataset to download, e.g. {DATASET_NAME}")
    importer.add_argument("--revision", help="dataset commit to download (default: current main, recorded)")
    importer.add_argument("--dataset-name", default=DATASET_NAME,
                          help="dataset name recorded for --rows-json (default: %(default)s)")
    importer.add_argument("--ids", required=True, help="IDs file or comma-separated IDs (ordered)")
    importer.add_argument("--out", type=Path, required=True, help="new task-repo directory (must not exist)")
    images = commands.add_parser("images", help="print the task images of an imported repo, one per line")
    images.add_argument("--task-repo", type=Path, required=True)
    images.add_argument("--ids", help="IDs file or comma-separated IDs (default: all imported)")
    gold = commands.add_parser("validate-gold", help="grade the gold patches through grade_driver.py (Docker)")
    gold.add_argument("--task-repo", type=Path, required=True)
    gold.add_argument("--ids", help="IDs file or comma-separated IDs (default: all imported)")
    gold.add_argument("--run-id", required=True)
    gold.add_argument("--workers", type=int, default=2)
    gold.add_argument("--timeout", type=int, default=1800, help="per-task test timeout seconds")
    gold.add_argument("--work-dir", type=Path,
                      help="new directory for logs and reports (default: <task-repo>/validation/<run-id>)")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "import":
            ids = parse_ids(args.ids)
            if args.dataset:
                if args.revision is not None and not re.fullmatch(r"[0-9a-f]{40}", args.revision):
                    raise ValueError("--revision must be a 40-hex dataset commit")
                rows, revision = load_dataset_rows(args.dataset, args.revision)
                dataset, source = args.dataset, {"kind": "huggingface", "revision": revision}
            else:
                if args.revision is not None:
                    raise ValueError("--revision applies to --dataset only")
                rows = read_json(args.rows_json)
                if not isinstance(rows, list):
                    raise ValueError("--rows-json must hold a JSON list of rows")
                dataset, source = args.dataset_name, {"kind": "rows-json", "sha256": sha256_file(args.rows_json)}
            manifest = write_task_repo(args.out, rows, ids, dataset, source)
            print(f"imported {len(ids)} task(s) from {dataset} into {args.out.absolute()}")
            if source["kind"] == "huggingface":
                print(f"dataset revision {manifest['source']['revision']}")
        elif args.command == "images":
            manifest = verified_manifest(args.task_repo, parse_ids(args.ids) if args.ids else None)
            for instance_id in (parse_ids(args.ids) if args.ids else manifest["instance_ids"]):
                print(manifest["tasks"][instance_id]["image"])
        else:
            manifest = verified_manifest(args.task_repo)
            ids = parse_ids(args.ids) if args.ids else manifest["instance_ids"]
            work = args.work_dir or args.task_repo / "validation" / args.run_id
            return validate_gold(args.task_repo, ids, args.run_id, args.workers, args.timeout, work)
    except (ValueError, OSError, ImportError, KeyError) as error:
        print(f"pepi-eval: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
