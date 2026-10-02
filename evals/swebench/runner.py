"""Build disposable Pi/task images and run one sequential, bounded attempt per task."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import signal
import stat
import subprocess
import time
import uuid
from pathlib import Path

from .common import UID, read_json, read_manifest, write_json

CREDENTIALS = ("LITELLM_BASE_URL", "LITELLM_API_KEY")
# mcp.json is not injected: MCP is always dropped from the profile (see prepare.declares_mcp).
SECRET_FILES = ("auth.json",)
SECRET_DIR = "/run/pepi-secrets"  # tmpfs inside the container; never part of an image layer
AGENT_DIR = "/opt/pepi-agent"
EXIT_OK, EXIT_USAGE, EXIT_INFRA, EXIT_INTERRUPTED = 0, 2, 3, 130
# Any Docker network that is not an isolated, operator-managed one.
FORBIDDEN_NETWORKS = ("host", "bridge", "default")
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
        log_path = run / f"build-{task['instance_id']}.log"
        try:
            with log_path.open("wb") as log:
                docker.call("build", "--platform", platform_name, "--tag", image,
                            "--build-arg", f"NODE_IMAGE={manifest['node_image']}",
                            "--build-arg", f"TASK_IMAGE={task['image']}",
                            "--build-arg", f"PI_VERSION={manifest['versions']['pi']}",
                            "--build-arg", f"LITELLM_VERSION={manifest['versions']['pi-provider-litellm']}",
                            "--build-arg", f"SUBAGENTS_VERSION={manifest['versions']['pi-subagents']}",
                            str(run / "build"), stdout=log, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f"docker build failed for {task['instance_id']} "
                               f"(exit {error.returncode}); see {log_path}") from None
        builds[task["instance_id"]] = {
            "image": image, "image_id": docker.text("image", "inspect", "--format", "{{.Id}}", image),
            "platform": platform_name, "build_seconds": time.monotonic() - started,
        }
        write_json(run / "builds.partial.json", builds)
    write_json(run / "builds.json", builds)


def stop_agents(docker: Docker, container: str) -> None:
    # Root controller only; Pi and all its descendants use this dedicated UID.
    # Docker.call is deliberately check=True; pkill/pgrep exit 1 means no process matched.
    # Kill inside the loop: a child forked between pkill's scan and its signal must still die.
    deadline = time.monotonic() + 10
    while True:
        try:
            docker.call("exec", container, "pkill", "-KILL", "-u", UID, capture_output=True)
        except subprocess.CalledProcessError as error:
            if error.returncode != 1:
                raise
        try:
            docker.call("exec", container, "pgrep", "-u", UID, capture_output=True)
        except subprocess.CalledProcessError as error:
            if error.returncode == 1:
                return
            raise
        if time.monotonic() >= deadline:
            raise RuntimeError("Agent processes did not stop; refusing a racy patch export")
        time.sleep(0.05)


def export_patch(docker: Docker, container: str, baseline: str) -> bytes:
    # Run as the unprivileged agent UID, including any repository Git filters.
    docker.call("exec", "--user", UID, "--workdir", "/testbed", container,
                "git", "-c", "core.fsmonitor=false", "add", "-A", capture_output=True)
    # The agent owns /testbed and $HOME, so pin the output format against its git config.
    patch = docker.call("exec", "--user", UID, "--workdir", "/testbed", container,
                        "git", "-c", "core.fsmonitor=false", "diff", "--cached", "--binary", "--no-color", "--no-ext-diff",
                        "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/", baseline, "--",
                        capture_output=True)
    return patch.stdout


class Secrets:
    """Secret files to inject at run time, plus every value that must be redacted."""

    def __init__(self, files: dict[str, bytes] | None = None):
        self.files = files or {}
        values = {os.environ[name] for name in CREDENTIALS[1:] if os.environ.get(name)}
        for content in self.files.values():
            try:
                values.update(self._leaves(json.loads(content)))
            except ValueError:
                values.add(content.decode("utf-8", "replace").strip())
        # Longest first so a value containing another is fully replaced.
        self.values = sorted((v.encode() for v in values if len(v) >= 8), key=len, reverse=True)

    @classmethod
    def _leaves(cls, node):
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            for child in node.values():
                yield from cls._leaves(child)
        elif isinstance(node, list):
            for child in node:
                yield from cls._leaves(child)

    @classmethod
    def load(cls, directory: Path | None) -> "Secrets":
        if directory is None:
            return cls()
        files = {}
        for path in sorted(directory.iterdir()):
            if path.name not in SECRET_FILES or not path.is_file() or path.is_symlink():
                raise ValueError(f"Unexpected entry in the secrets directory: {path.name} "
                                 f"(allowed files: {', '.join(SECRET_FILES)})")
            if stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise ValueError(f"{path.name} is readable by group/others; run chmod 600")
            files[path.name] = path.read_bytes()
        return cls(files)

    def redact(self, data: bytes) -> tuple[bytes, int]:
        count = 0
        for value in self.values:
            count += data.count(value)
            data = data.replace(value, b"<redacted>")
        return data, count

    def redact_text(self, text: str) -> str:
        return self.redact(text.encode("utf-8", "replace"))[0].decode("utf-8", "replace")

    def redact_tree(self, root: Path) -> tuple[int, list[str]]:
        """Redact every regular file under `root`; fail closed.

        Symlinks and special files are deleted (docker cp preserves links, and an absolute
        target would make an artifact uploader read a host file). A file that cannot be read
        or redacted is deleted rather than left raw. Returns (redactions, removed paths).
        """
        total, removed = 0, []
        for path in sorted(root.rglob("*"), reverse=True):  # children before parents
            try:
                if path.is_symlink() or (path.exists() and not path.is_dir() and not path.is_file()):
                    path.unlink()
                    removed.append(path.relative_to(root).as_posix())
                elif path.is_file() and self.values:
                    path.chmod(0o600)  # docker cp keeps the agent's mode bits
                    original = path.read_bytes()
                    redacted, count = self.redact(original)
                    if count:
                        path.write_bytes(redacted)
                        total += count
            except Exception:
                try:
                    path.unlink()
                except OSError:
                    pass
                removed.append(path.relative_to(root).as_posix())
        return total, removed

    def inject(self, docker: Docker, container: str) -> None:
        # Values travel over stdin only: never argv, env, `docker cp` (unsupported on tmpfs) or logs.
        for name, content in self.files.items():
            docker.call("exec", "-i", "--user", UID, container, "sh", "-c",
                        f"umask 077; cat > {SECRET_DIR}/{name}", input=content, capture_output=True)
            docker.call("exec", "--user", UID, container, "ln", "-sf",
                        f"{SECRET_DIR}/{name}", f"{AGENT_DIR}/{name}", capture_output=True)


def decode_patch(raw: bytes, record: dict) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        record["patch_encoding"] = "non-utf8"
        record["errors"].append("Patch is not valid UTF-8: predictions.jsonl holds a lossy copy; "
                                "patch.diff holds the exact bytes")
        return raw.decode("utf-8", "replace")


def run_one(docker: Docker, task: dict, build: dict, output: Path, timeout: float,
            network: str, cpus: str, memory: str, secrets: Secrets | None = None,
            run_id: str = "") -> dict:
    secrets = secrets or Secrets()
    container = f"pepi-eval-{uuid.uuid4().hex}"
    record = {"schema_version": 1, "instance_id": task["instance_id"], "attempt_id": 1,
              "agent_status": "setup_error", "patch": "", "timing_seconds": {}, "errors": [],
              "secret_redactions": 0}
    raw_patch = b""
    setup = time.monotonic()
    started = None
    created = False
    create_returned = False
    try:
        output.mkdir(parents=True, exist_ok=False)
        issue = output / "issue.md"
        issue.write_text(task["problem_statement"], encoding="utf-8")
        issue.chmod(0o644)  # docker cp keeps the mode; the agent UID must be able to read it
        tmpfs = (["--tmpfs", f"{SECRET_DIR}:rw,noexec,nosuid,size=1m,mode=0700,uid={UID},gid={UID}"]
                 if secrets.files else [])
        label = ["--label", f"pepi-eval.run={run_id}"] if run_id else []
        try:
            docker.call("create", "--name", container, "--init", "--network", network, *label,
                        "--cap-drop", "ALL", "--cap-add", "KILL", "--security-opt", "no-new-privileges",
                        "--pids-limit", "512", "--cpus", cpus, "--memory", memory, *tmpfs,
                        "--entrypoint", "sleep", build["image_id"], "infinity", capture_output=True)
            create_returned = True
        finally:
            created = True  # a timed-out or interrupted create may still have made the container
        docker.call("start", container, capture_output=True)
        secrets.inject(docker, container)
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
                # PEPI_EVAL_OUTPUT is consumed by the telemetry extension in a later PR.
                docker.call("exec", "--user", UID, "--workdir", "/testbed", *env,
                            "--env", "PEPI_EVAL_OUTPUT=/output", container,
                            "bash", "-c", PI_COMMAND, stdout=stdout, stderr=stderr, timeout=timeout)
            except subprocess.TimeoutExpired:
                record["agent_status"] = "timeout"
            except subprocess.CalledProcessError as error:
                record["agent_status"] = "agent_error"
                record["errors"].append(f"Pi exit code: {error.returncode}")
        stop_agents(docker, container)
        raw_patch = export_patch(docker, container, baseline)
        record["timing_seconds"]["agent_wall"] = time.monotonic() - started
        stop_agents(docker, container)
    except KeyboardInterrupt:
        record["agent_status"] = "interrupted"
    except Exception as error:
        record["agent_status"] = "runner_error" if started is not None else "setup_error"
        # Exception text can embed argv/paths: redact it like every other artifact.
        record["errors"].append(secrets.redact_text(f"{type(error).__name__}: {error}"))
    finally:
        if started is not None:
            record["timing_seconds"].setdefault("agent_wall", time.monotonic() - started)
        else:
            record["timing_seconds"]["setup"] = time.monotonic() - setup
        # A second cancel signal (SIGINT then SIGTERM) must not skip cleanup or redaction.
        blocked = {signal.SIGINT, signal.SIGTERM}
        signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
        try:
            _finish_attempt(docker, container, created, create_returned, output, record,
                            raw_patch, secrets)
        finally:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, blocked)
    return record


def _finish_attempt(docker, container, created, create_returned, output, record, raw_patch, secrets):
    if created:
        try:
            if create_returned:
                stop_agents(docker, container)
                artifacts = output / "artifacts"
                artifacts.mkdir(parents=True, exist_ok=True)
                docker.call("cp", f"{container}:/output/.", str(artifacts), capture_output=True)
        except Exception as error:
            record["errors"].append(secrets.redact_text(f"Artifact collection failed: {error}"))
        finally:
            try:
                docker.call("rm", "--force", container, capture_output=True)
            except Exception as error:
                if create_returned:
                    record["errors"].append(secrets.redact_text(f"Cleanup failed for {container}: {error}"))
    try:
        output.mkdir(parents=True, exist_ok=True)
        redactions, removed = secrets.redact_tree(output)
        raw_patch, patch_hits = secrets.redact(raw_patch)
        record["secret_redactions"] = redactions + patch_hits
        if removed:
            record["removed_unredactable"] = removed
        record["patch"] = decode_patch(raw_patch, record)
        (output / "patch.diff").write_bytes(raw_patch)
        write_json(output / "attempt.json", {k: v for k, v in record.items() if k != "patch"})
    except Exception as error:  # never lose the prediction row; never leave raw artifacts
        record["errors"].append(secrets.redact_text(f"Attempt bookkeeping failed: {error}"))
        record["agent_status"] = "runner_error"
        for name in ("artifacts", "parent.events.jsonl", "parent.stderr.log", "patch.diff"):
            target = output / name
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)


def run_tasks(run: Path, timeout: float, network: str, cpus: str, memory: str,
              secrets_dir: Path | None = None) -> int:
    """Return EXIT_OK when every attempt ran (even if the agent failed or timed out),
    EXIT_INFRA when any attempt hit a setup/runner error, EXIT_INTERRUPTED on Ctrl-C/SIGTERM."""
    manifest = read_manifest(run)
    builds = read_json(run / "builds.json")
    if network in FORBIDDEN_NETWORKS or network.startswith("container:"):
        raise ValueError("Use an isolated operator-managed Docker network, not host/bridge/default/container:*")
    if any(not os.environ.get(name) for name in CREDENTIALS):
        raise ValueError("Set LITELLM_BASE_URL and LITELLM_API_KEY; do not use sudo")
    if (run / "predictions.jsonl").exists() or (run / "attempts").exists():
        raise ValueError("Attempts already exist; use a fresh run directory")
    if set(builds) != {task["instance_id"] for task in manifest["tasks"]}:
        raise ValueError("Build records do not cover the selected tasks")
    # A network ID or alias must not bypass the name check above.
    try:
        resolved = Docker().text("network", "inspect", "--format", "{{.Name}}", network)
    except subprocess.CalledProcessError:
        raise ValueError(f"Docker network not found: {network}") from None
    if resolved in FORBIDDEN_NETWORKS:
        raise ValueError(f"Network {network} resolves to {resolved}; use an isolated operator-managed network")
    secrets = Secrets.load(secrets_dir)
    write_json(run / "execution.json", {
        "timeout_seconds": timeout, "network": network, "network_policy": "operator-managed; not verified",
        "cpus": float(cpus), "memory": memory, "concurrency": 1, "host": platform.platform(),
        "pids_limit": 512, "timeout_scope": "Pi invocation; setup and export are outside this deadline",
        "timeout_patch_policy": "submit partial diff after stopping all agent processes",
        # Names and presence only: never values or hashes of secrets.
        "secrets": {name: {"present": name in secrets.files} for name in SECRET_FILES},
        "config_label": manifest.get("config", {}).get("label"),
    })
    infra_failed = False
    interrupted = False
    label = manifest["config"].get("label")
    model_name = f"{label}-{manifest['config_id']}" if label else manifest["config_id"]
    with (run / "predictions.jsonl").open("x", encoding="utf-8") as predictions:
        for task in manifest["tasks"]:
            if interrupted:
                row = {"instance_id": task["instance_id"], "agent_status": "not_started", "patch": ""}
            else:
                try:
                    row = run_one(Docker(), task, builds[task["instance_id"]],
                                  run / "attempts" / task["instance_id"], timeout, network, cpus,
                                  memory, secrets, manifest["run_id"])
                except Exception as error:  # keep every selected ID in predictions.jsonl
                    row = {"instance_id": task["instance_id"], "agent_status": "runner_error", "patch": "",
                           "errors": [secrets.redact_text(f"{type(error).__name__}: {error}")]}
            infra_failed = infra_failed or row["agent_status"] in ("setup_error", "runner_error")
            interrupted = interrupted or row["agent_status"] == "interrupted"
            predictions.write(json.dumps({"instance_id": task["instance_id"],
                "model_name_or_path": model_name, "model_patch": row["patch"]}) + "\n")
            predictions.flush()
            os.fsync(predictions.fileno())
    return EXIT_INTERRUPTED if interrupted else EXIT_INFRA if infra_failed else EXIT_OK
