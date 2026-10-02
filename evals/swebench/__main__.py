"""Usage: python -m evals.swebench --help (Python 3.10+)."""
import argparse
from pathlib import Path

from .common import DATASET, git_revision, positive
from .prepare import prepare_run, selected_ids
from .runner import build_images, run_tasks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="freeze a task selection and local-only profile")
    prepare.add_argument("--pepi-root", type=Path, default=Path(__file__).resolve().parents[2])
    prepare.add_argument("--task-repo", type=Path, required=True)
    prepare.add_argument("--ids", type=Path, required=True)
    prepare.add_argument("--out", type=Path, required=True)
    prepare.add_argument("--dataset", default=DATASET)
    prepare.add_argument("--pi-version", required=True)
    prepare.add_argument("--litellm-version", required=True)
    prepare.add_argument("--subagents-version", required=True)
    prepare.add_argument("--node-image", required=True, help="prefer a digest-pinned Node 22 Debian image")
    build = commands.add_parser("build", help="build Pi on top of each task image")
    build.add_argument("run", type=Path)
    build.add_argument("--platform", default="linux/amd64")
    run = commands.add_parser("run", help="run Pi; this makes paid gateway requests")
    run.add_argument("run", type=Path)
    run.add_argument("--timeout-seconds", type=positive, required=True)
    run.add_argument("--network", required=True, help="Docker network with operator-enforced gateway-only egress")
    run.add_argument("--cpus", type=positive, default=4)
    run.add_argument("--memory", default="8g")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            # Import only here: tests/reporting do not require the grading package.
            from swebench.task.repo import load_task_repo
            ids = selected_ids(args.ids)
            prepare_run(args.pepi_root.resolve(), args.out.resolve(),
                        load_task_repo(args.task_repo, ids), ids,
                        {"pi": args.pi_version, "pi-provider-litellm": args.litellm_version,
                         "pi-subagents": args.subagents_version}, args.node_image, args.dataset,
                        git_revision(args.task_repo))
        elif args.command == "build":
            build_images(args.run.resolve(), args.platform)
        elif args.command == "run":
            return run_tasks(args.run.resolve(), args.timeout_seconds, args.network, str(args.cpus), args.memory)
    except (ValueError, OSError, ImportError, KeyError) as error:
        parser.exit(2, f"pepi-eval: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
