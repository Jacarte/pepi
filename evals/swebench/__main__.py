"""Usage: python -m evals.swebench --help (Python 3.10+).

Exit codes: 0 all attempts ran (agent failures/timeouts are benchmark outcomes),
2 usage/configuration error, 3 infrastructure failure (setup/runner error),
130 interrupted (Ctrl-C or SIGTERM).
"""
import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 10):
    raise SystemExit("pepi-eval: Python 3.10+ is required")

from .common import DATASET, git_revision, non_negative, positive  # noqa: E402
from .prepare import prepare_run, selected_ids  # noqa: E402
from .preset import (DEFAULT_IDS, DEFAULT_PRESET, MULTILINGUAL_IDS, PRESET_NAMES, definition,  # noqa: E402
                     multilingual_definition, preset_dataset, preset_ids, requires_task_inputs,
                     task_input_fingerprints)
from .runner import EXIT_INFRA, EXIT_USAGE, HISTORY_PRUNE_TIMEOUT, build_images, run_tasks  # noqa: E402


class ExplicitIds(argparse.Action):
    """Records that --ids was given, so --preset only replaces the default IDs file."""

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        namespace.ids_explicit = True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="freeze a task selection and isolated profile")
    prepare.add_argument("--pepi-root", type=Path, default=Path(__file__).resolve().parents[2],
                         help="checkout providing default config files")
    prepare.add_argument("--config-dir", type=Path,
                         help="injected config (settings.json, models.json, AGENTS.md, workflows/, "
                              "extensions/subagent/config.json, mcp-packages.json + mcp-package-lock.json); "
                              "precedence: injected > named profile > --pepi-root (named models never use root fallback). "
                              "Must not contain auth.json/mcp.json: use `run --secrets-dir`")
    prepare.add_argument("--profile", default=os.environ.get("PEPI_EVAL_PROFILE"),
                         help="named profiles/NAME selection, without activating it")
    prepare.add_argument("--config-label", help="name recorded in the manifest and predictions")
    prepare.add_argument("--drop-mcp", action="store_true",
                         help="disable native MCP explicitly (enabled by default with Pi >= 1.1.0)")
    prepare.add_argument("--task-repo", type=Path, required=True)
    prepare.add_argument("--preset", choices=PRESET_NAMES, default=DEFAULT_PRESET,
                         help="frozen preset supplying the default --ids and --dataset (default: %(default)s)")
    prepare.add_argument("--ids", type=Path, default=DEFAULT_IDS, action=ExplicitIds,
                         help="ordered IDs file (default: the preset's IDs; frozen custom Verified20 v1)")
    prepare.set_defaults(ids_explicit=False)
    prepare.add_argument("--out", type=Path, required=True)
    prepare.add_argument("--dataset", help=f"dataset name (default: the preset's; {DATASET})")
    prepare.add_argument("--pi-version", required=True)
    prepare.add_argument("--litellm-version", help="exact pin required only for LiteLLM models")
    prepare.add_argument("--subagents-version", required=True)
    prepare.add_argument("--node-image", required=True, help="prefer a digest-pinned Node 22 Debian image")
    build = commands.add_parser("build", help="build Pi on top of each task image")
    build.add_argument("run", type=Path)
    build.add_argument("--platform", default="linux/amd64")
    run = commands.add_parser("run", help="run Pi; this makes paid provider requests")
    run.add_argument("run", type=Path)
    run.add_argument("--timeout-seconds", type=positive, default=300,
                     help="per-task Pi invocation deadline, excluding setup/export (default: 300)")
    run.add_argument("--network", required=True,
                     help="Docker network with operator-enforced gateway/MCP endpoint allowlist")
    run.add_argument("--cpus", type=positive, default=4)
    run.add_argument("--memory", default="8g")
    run.add_argument("--secrets-dir", type=Path,
                     help="directory with chmod-600 auth.json and/or mcp.json; injected into a tmpfs at run time")
    run.add_argument("--pass-env", action="append", default=[], metavar="NAME",
                     help="forward this environment variable into the task container by name (repeatable); "
                          "the value comes from this process's environment, is readable by the agent and "
                          "its MCP servers, and is redacted from artifacts")
    run.add_argument("--skills-dir", type=Path,
                     help="chmod-700 directory of skill directories (each with SKILL.md); mounted read-only "
                          "at /opt/pepi-agent/skills for Pi to discover")
    run.add_argument("--history-prune-timeout", type=non_negative, default=HISTORY_PRUNE_TIMEOUT,
                     help="seconds for removing git refs/objects after the task base before the agent starts "
                          f"(0 disables; default: {HISTORY_PRUNE_TIMEOUT:g})")
    return parser


def _raise_interrupt(*_):
    raise KeyboardInterrupt  # CI cancellation sends SIGTERM: unwind so containers are removed


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, _raise_interrupt)
    try:
        if args.command == "prepare":
            default = args.preset == DEFAULT_PRESET
            ids_path = args.ids if args.ids_explicit or default else preset_ids(args.preset)
            dataset = args.dataset or (DATASET if default else preset_dataset(args.preset))
            # A changed shipped default must fail, not become an ordinary custom selection.
            if ids_path.resolve() == DEFAULT_IDS.resolve():
                definition()
            elif ids_path.resolve() == MULTILINGUAL_IDS.resolve():
                multilingual_definition()
            # Import only here: tests/reporting do not require the grading package.
            from swebench.task.repo import load_task_repo
            ids = selected_ids(ids_path)
            task_inputs = task_input_fingerprints(args.task_repo, ids) if requires_task_inputs(ids) else None
            prepare_run(args.pepi_root.resolve(), args.out.resolve(),
                        load_task_repo(args.task_repo, ids), ids,
                        {"pi": args.pi_version, "pi-provider-litellm": args.litellm_version,
                         "pi-subagents": args.subagents_version}, args.node_image, dataset,
                        git_revision(args.task_repo),
                        args.config_dir.absolute() if args.config_dir else None,
                        args.config_label, args.drop_mcp, args.profile, task_inputs)
        elif args.command == "build":
            build_images(args.run.resolve(), args.platform)
        elif args.command == "run":
            return run_tasks(args.run.resolve(), args.timeout_seconds, args.network, str(args.cpus),
                             args.memory, args.secrets_dir.absolute() if args.secrets_dir else None,
                             args.pass_env, args.skills_dir.absolute() if args.skills_dir else None,
                             history_prune_timeout=args.history_prune_timeout)
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
