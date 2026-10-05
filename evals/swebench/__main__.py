"""Usage: python -m evals.swebench --help (Python 3.10+).

Exit codes: 0 all attempts ran (agent failures/timeouts are benchmark outcomes),
2 usage/configuration error, 3 infrastructure failure (setup/runner error),
130 interrupted (Ctrl-C or SIGTERM).
"""
import argparse
import signal
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 10):
    raise SystemExit("pepi-eval: Python 3.10+ is required")

from .common import DATASET, git_revision, positive  # noqa: E402
from .prepare import prepare_run, selected_ids  # noqa: E402
from .runner import EXIT_INFRA, EXIT_USAGE, build_images, run_tasks  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="freeze a task selection and local-only profile")
    prepare.add_argument("--pepi-root", type=Path, default=Path(__file__).resolve().parents[2],
                         help="checkout providing default config files")
    prepare.add_argument("--config-dir", type=Path,
                         help="injected config (settings.json, AGENTS.md, workflows/, "
                              "extensions/subagent/config.json); missing files fall back to --pepi-root. "
                              "Must not contain auth.json/mcp.json: use `run --secrets-dir`")
    prepare.add_argument("--config-label", help="name recorded in the manifest and predictions")
    prepare.add_argument("--drop-mcp", action="store_true",
                         help="allow a config that declares MCP; MCP is removed from the profile")
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
    run.add_argument("--secrets-dir", type=Path,
                     help="directory with chmod-600 auth.json and/or mcp.json; injected into a tmpfs at run time")
    return parser


def _raise_interrupt(*_):
    raise KeyboardInterrupt  # CI cancellation sends SIGTERM: unwind so containers are removed


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, _raise_interrupt)
    try:
        if args.command == "prepare":
            # Import only here: tests/reporting do not require the grading package.
            from swebench.task.repo import load_task_repo
            ids = selected_ids(args.ids)
            prepare_run(args.pepi_root.resolve(), args.out.resolve(),
                        load_task_repo(args.task_repo, ids), ids,
                        {"pi": args.pi_version, "pi-provider-litellm": args.litellm_version,
                         "pi-subagents": args.subagents_version}, args.node_image, args.dataset,
                        git_revision(args.task_repo),
                        args.config_dir.resolve() if args.config_dir else None,
                        args.config_label, args.drop_mcp)
        elif args.command == "build":
            build_images(args.run.resolve(), args.platform)
        elif args.command == "run":
            return run_tasks(args.run.resolve(), args.timeout_seconds, args.network, str(args.cpus),
                             args.memory, args.secrets_dir.resolve() if args.secrets_dir else None)
    except (ValueError, OSError, ImportError, KeyError) as error:
        print(f"pepi-eval: {error}", file=sys.stderr)
        return EXIT_USAGE
    except (subprocess.SubprocessError, RuntimeError) as error:
        print(f"pepi-eval: {error}", file=sys.stderr)
        return EXIT_INFRA
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
