"""Build disposable Pi/task images and run one sequential, bounded attempt per task."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import time
import uuid
from pathlib import Path

from .common import UID, read_json, read_manifest, write_json

CREDENTIALS = ("LITELLM_BASE_URL", "LITELLM_API_KEY")
PI_COMMAND = (
    "source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && "
    "exec pi --print --mode json --session-dir /output/sessions "
    "--append-system-prompt /opt/pepi-eval/instructions.md @/input/issue.md"
)


class Docker:
    def call(self, *args: str, **kwargs):
        kwargs.setdefault("timeout", None if args and args[0] == "build" else 120)
        return subprocess.run(["docker", *args], check=True, **kwargs)

    def text(self, *args: str) -> str:
        return self.call(*args, capture_output=True).stdout.decode("utf-8").strip()


def build_images(run: Path, platform_name: str) -> None:
    manifest = read_manifest(run)
    if (run / "builds.json").exists():
        raise ValueError("Build records already exist; prepare a new run")
    docker = Docker()
    builds = {}
    for task in manifest["tasks"]:
        suffix = hashlib.sha256(task["instance_id"].encode()).hexdigest()[:16]
        image = f"pepi-eval:{manifest['config_id']}-{suffix}"
        started = time.monotonic()
        with (run / f"build-{task['instance_id']}.log").open("wb") as log:
            docker.call("build", "--platform", platform_name, "--tag", image,
                        "--build-arg", f"NODE_IMAGE={manifest['node_image']}",
                        "--build-arg", f"TASK_IMAGE={task['image']}",
                        "--build-arg", f"PI_VERSION={manifest['versions']['pi']}",
                        "--build-arg", f"LITELLM_VERSION={manifest['versions']['pi-provider-litellm']}",
                        "--build-arg", f"SUBAGENTS_VERSION={manifest['versions']['pi-subagents']}",
                        str(run / "build"), stdout=log, stderr=subprocess.STDOUT)
        builds[task["instance_id"]] = {
            "image": image, "image_id": docker.text("image", "inspect", "--format", "{{.Id}}", image),
            "platform": platform_name, "build_seconds": time.monotonic() - started,
        }
        write_json(run / "builds.partial.json", builds)
    write_json(run / "builds.json", builds)


def stop_agents(docker: Docker, container: str) -> None:
    # Root controller only; Pi and all its descendants use this dedicated UID.
    # Docker.call is deliberately check=True; pkill's 1 means no processes matched.
    try:
        docker.call("exec", container, "pkill", "-KILL", "-u", UID, capture_output=True)
    except subprocess.CalledProcessError as error:
        if error.returncode != 1:
            raise
    deadline = time.monotonic() + 10
    while True:
        try:
            docker.call("exec", container, "pgrep", "-u", UID, capture_output=True)
        except subprocess.CalledProcessError as error:
            if error.returncode == 1:
                return
            raise
        if time.monotonic() >= deadline:
            raise RuntimeError("Agent processes did not stop; refusing a racy patch export")
        time.sleep(0.05)


def export_patch(docker: Docker, container: str, baseline: str) -> str:
    # Run as the unprivileged agent UID, including any repository Git filters.
    docker.call("exec", "--user", UID, "--workdir", "/testbed", container,
                "git", "add", "-A", capture_output=True)
    patch = docker.call("exec", "--user", UID, "--workdir", "/testbed", container,
                        "git", "diff", "--cached", "--binary", baseline, "--", capture_output=True)
    return patch.stdout.decode("utf-8")


def run_one(docker: Docker, task: dict, build: dict, output: Path,
            timeout: float, network: str, cpus: str, memory: str) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    issue = output / "issue.md"
    issue.write_text(task["problem_statement"], encoding="utf-8")
    container = f"pepi-eval-{uuid.uuid4().hex}"
    record = {"schema_version": 1, "instance_id": task["instance_id"], "attempt_id": 1,
              "agent_status": "setup_error", "patch": "", "timing_seconds": {}, "errors": []}
    setup = time.monotonic()
    started = None
    created = False
    try:
        docker.call("create", "--name", container, "--init", "--network", network,
                    "--cap-drop", "ALL", "--cap-add", "KILL", "--security-opt", "no-new-privileges",
                    "--pids-limit", "512", "--cpus", cpus, "--memory", memory,
                    "--entrypoint", "sleep", build["image_id"], "infinity", capture_output=True)
        created = True
        docker.call("start", container, capture_output=True)
        docker.call("cp", str(issue), f"{container}:/input/issue.md", capture_output=True)
        baseline = docker.text("exec", "--user", UID, "--workdir", "/testbed", container,
                               "git", "rev-parse", "HEAD")
        # Official images may add an empty setup commit; compare trees, not HEAD IDs.
        delta = docker.text("exec", "--user", UID, "--workdir", "/testbed", container,
                            "git", "diff", "--stat", task["base_commit"], baseline, "--")
        dirty = docker.text("exec", "--user", UID, "--workdir", "/testbed", container,
                            "git", "status", "--porcelain")
        if delta or dirty:
            raise ValueError("Task checkout does not match its clean baseline")
        record["baseline_commit"] = baseline
        record["timing_seconds"]["setup"] = time.monotonic() - setup
        env = [value for name in CREDENTIALS for value in ("--env", name)]
        started = time.monotonic()
        record["agent_status"] = "completed"
        with (output / "parent.events.jsonl").open("wb") as stdout, \
             (output / "parent.stderr.log").open("wb") as stderr:
            try:
                docker.call("exec", "--user", UID, "--workdir", "/testbed", *env,
                            "--env", "PEPI_EVAL_OUTPUT=/output", container,
                            "bash", "-c", PI_COMMAND, stdout=stdout, stderr=stderr, timeout=timeout)
            except subprocess.TimeoutExpired:
                record["agent_status"] = "timeout"
            except subprocess.CalledProcessError as error:
                record["agent_status"] = "agent_error"
                record["errors"].append(f"Pi exit code: {error.returncode}")
        stop_agents(docker, container)
        record["patch"] = export_patch(docker, container, baseline)
        record["timing_seconds"]["agent_wall"] = time.monotonic() - started
        stop_agents(docker, container)
    except KeyboardInterrupt:
        record["agent_status"] = "interrupted"
    except Exception as error:
        record["agent_status"] = "runner_error" if started is not None else "setup_error"
        record["errors"].append(f"{type(error).__name__}: {error}")
    finally:
        if started is not None:
            record["timing_seconds"].setdefault("agent_wall", time.monotonic() - started)
        else:
            record["timing_seconds"]["setup"] = time.monotonic() - setup
        if created:
            try:
                stop_agents(docker, container)
                artifacts = output / "artifacts"
                artifacts.mkdir()
                docker.call("cp", f"{container}:/output/.", str(artifacts), capture_output=True)
            except Exception as error:
                record["errors"].append(f"Artifact collection failed: {error}")
            finally:
                try:
                    docker.call("rm", "--force", container, capture_output=True)
                except Exception as error:
                    record["errors"].append(f"Cleanup failed for {container}: {error}")
        (output / "patch.diff").write_text(record["patch"], encoding="utf-8", newline="")
        write_json(output / "attempt.json", {k: v for k, v in record.items() if k != "patch"})
    return record


def run_tasks(run: Path, timeout: float, network: str, cpus: str, memory: str) -> int:
    manifest = read_manifest(run)
    builds = read_json(run / "builds.json")
    if network == "host" or network.startswith("container:"):
        raise ValueError("Host/shared-container networking is not supported")
    if any(not os.environ.get(name) for name in CREDENTIALS):
        raise ValueError("Set LITELLM_BASE_URL and LITELLM_API_KEY; do not use sudo")
    if (run / "predictions.jsonl").exists() or (run / "attempts").exists():
        raise ValueError("Attempts already exist; use a fresh run directory")
    if set(builds) != {task["instance_id"] for task in manifest["tasks"]}:
        raise ValueError("Build records do not cover the selected tasks")
    write_json(run / "execution.json", {
        "timeout_seconds": timeout, "network": network, "network_policy": "operator-managed; not verified",
        "cpus": float(cpus), "memory": memory, "concurrency": 1, "host": platform.platform(),
        "pids_limit": 512, "timeout_scope": "Pi invocation; setup and export are outside this deadline",
        "timeout_patch_policy": "submit partial diff after stopping all agent processes",
    })
    failed = 0
    interrupted = False
    with (run / "predictions.jsonl").open("x", encoding="utf-8") as predictions:
        for task in manifest["tasks"]:
            if interrupted:
                row = {"instance_id": task["instance_id"], "agent_status": "not_started", "patch": ""}
            else:
                row = run_one(Docker(), task, builds[task["instance_id"]],
                              run / "attempts" / task["instance_id"], timeout, network, cpus, memory)
            failed += row["agent_status"] != "completed"
            interrupted = interrupted or row["agent_status"] == "interrupted"
            predictions.write(json.dumps({"instance_id": task["instance_id"],
                "model_name_or_path": manifest["config_id"], "model_patch": row["patch"]}) + "\n")
            predictions.flush()
            os.fsync(predictions.fileno())
    return 1 if failed else 0
