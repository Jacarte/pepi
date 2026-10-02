"""Run the official evaluator in a separate controller process, never in Pi."""
import hashlib
import importlib.metadata
import inspect
import json
import subprocess
import sys
from pathlib import Path


def main():
    from swebench.harness.run_evaluation import main as evaluate
    from swebench.task.repo import load_task_repo

    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    expected = config.pop("expected_tasks")
    tasks = load_task_repo(config["task_repo"], config["instance_ids"])
    by_id = {task["instance_id"]: task for task in tasks}
    if set(by_id) != set(config["instance_ids"]):
        raise ValueError("Frozen grading tasks differ from generation selection")
    for row in expected:
        task = by_id[row["instance_id"]]
        for key in ("repo", "base_commit", "image", "problem_statement"):
            if task[key] != row[key]:
                raise ValueError(f"Grading task changed since generation: {row['instance_id']} {key}")
        if config["dataset_name"] not in task.get("datasets", []) or task.get("split") != config["split"]:
            raise ValueError("Grading task dataset/split does not match generation")
    source = Path(inspect.getfile(evaluate)).resolve()
    revision = subprocess.run(["git", "-C", str(source.parent), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=False)
    Path("evaluator.json").write_text(json.dumps({
        "package_version": importlib.metadata.version("swebench"),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "git_revision": revision.stdout.strip() if revision.returncode == 0 else None,
    }, indent=2) + "\n", encoding="utf-8")
    evaluate(**config)


if __name__ == "__main__":
    main()
