"""The agent layer on non-Python task images: conda-optional Pi command and the Dockerfile (no Docker)."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from evals.swebench.runner import CONTROLLER_ENV, PI_COMMAND, PI_NODE
from evals.swebench.prepare import prepare_run
from evals.swebench.tests.test_runner import TASK, VERSIONS, prepare_fixture

CONDA_SH = "/opt/miniconda3/etc/profile.d/conda.sh"
DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"
INSTALL_MCP = Path(__file__).resolve().parents[1] / "install-mcp.mjs"
# Config.Env PATH of the local task images (docker image inspect), Python and Multilingual.
TASK_PATHS = {
    "sympy": "/opt/miniconda3/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "axios": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "tokio": "/usr/local/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "rubocop": "/usr/local/bundle/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
}


def instructions(stage: str) -> list[str]:
    """Dockerfile instructions with line continuations joined; comments dropped."""
    joined = re.sub(r"\\\n", " ", stage)
    return [line.strip() for line in joined.splitlines() if line.strip() and not line.lstrip().startswith("#")]


class PiCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.args_log = self.root / "pi-args"
        stub = self.root / "bin" / "pi"
        stub.parent.mkdir()
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "conda=${CONDA_ACTIVATED:-no}" "$@" > "$PI_ARGS_LOG"\n')
        stub.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{stub.parent}:{os.environ['PATH']}", PI_ARGS_LOG=str(self.args_log))
        self.env.pop("PEPI_EVAL_MCP_FLAG", None)

    def run_command(self, command):
        return subprocess.run(["bash", "-c", command], env=self.env, capture_output=True, timeout=30)

    def with_conda(self, body):
        conda = self.root / "conda.sh"
        conda.write_text(body)
        self.assertIn(CONDA_SH, PI_COMMAND)
        return PI_COMMAND.replace(CONDA_SH, str(conda))

    def test_image_without_conda_starts_pi_directly(self):
        if Path(CONDA_SH).exists():
            self.skipTest("this host has /opt/miniconda3")
        result = self.run_command(PI_COMMAND)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.args_log.read_text().splitlines(),
                         ["conda=no", "--print", "--mode", "json", "--session-dir", "/output/sessions",
                          "--append-system-prompt", "/opt/pepi-agent/AGENTS.md",
                          "--append-system-prompt", "/opt/pepi-eval/instructions.md", "@/input/issue.md"])

    def test_python_image_activates_the_testbed_env_before_pi(self):
        command = self.with_conda('conda() { [ "$1 $2" = "activate testbed" ] && export CONDA_ACTIVATED=1; }\n')
        result = self.run_command(command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.args_log.read_text().splitlines()[0], "conda=1")

    def test_failed_activation_stops_before_pi(self):
        result = self.run_command(self.with_conda("conda() { return 7; }\n"))
        self.assertEqual(result.returncode, 7)
        self.assertFalse(self.args_log.exists())

    def test_each_arm_appends_its_own_frozen_rules_before_common_safety(self):
        baseline = prepare_fixture(self.root)
        candidate_rules = "Candidate only: use cooperative_plan for nontrivial repairs.\n"
        (self.root / "pepi/AGENTS.md").write_text(candidate_rules)
        candidate = self.root / "candidate"
        prepare_run(self.root / "pepi", candidate, [TASK], [TASK["instance_id"]], VERSIONS, "node:22-bookworm-slim")
        self.env["PEPI_EVAL_MCP_FLAG"] = "--no-mcp"
        result = self.run_command(self.with_conda("conda() { return 0; }\n"))
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.args_log.read_text().splitlines()[1:]
        self.assertIn("--no-mcp", argv)
        paths = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--append-system-prompt"]
        self.assertEqual(paths, ["/opt/pepi-agent/AGENTS.md", "/opt/pepi-eval/instructions.md"])
        self.assertEqual(argv[-1], "@/input/issue.md")
        prompts = []
        for run in (baseline, candidate):
            files = {paths[0]: run / "build/profile/AGENTS.md", paths[1]: run / "build/instructions.md"}
            prompts.append("\n\n".join(files[path].read_text() for path in paths))
        self.assertTrue(prompts[0].startswith("Delegate; verify; report honestly.\n"))
        self.assertNotIn("cooperative_plan", prompts[0])
        self.assertTrue(prompts[1].startswith(candidate_rules))
        common = (DOCKERFILE.parent / "instructions.md").read_text()
        for prompt in prompts:
            self.assertTrue(prompt.endswith(common))
            self.assertIn("runtime limits take precedence over conflicting host working rules", prompt)
        self.assertEqual(json.loads((candidate / "manifest.json").read_text())["tasks"][0]["problem_statement"],
                         TASK["problem_statement"])

    def test_pi_is_the_last_command(self):
        self.assertTrue(PI_COMMAND.split("&& ")[-1].startswith("exec pi "))


class AgentDockerfileTests(unittest.TestCase):
    def setUp(self):
        text = DOCKERFILE.read_text()
        self.agent, separator, self.task = text.partition("FROM ${TASK_IMAGE}")
        self.assertTrue(separator)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def task_run(self, marker):
        return next(line for line in instructions(self.task) if line.startswith("RUN ") and marker in line)

    def test_final_path_resolves_pi_to_the_wrapper_and_node_to_the_task_first(self):
        paths = [line for line in instructions(self.task) if re.match(r"(ENV|ARG) PATH[= ]", line)]
        self.assertEqual(paths, ["ENV PATH=/opt/pepi-eval/bin:$PATH:/opt/pepi-node/bin"])
        template = paths[0][len("ENV PATH="):]
        for image, task_path in TASK_PATHS.items():
            with self.subTest(image=image):
                # Docker substitutes $PATH with the task image's; resolve under a fake root, as `command -v` would.
                fake = self.root / image
                tools = {"opt/pepi-eval/bin/pi": "wrapper", "opt/pepi-node/bin/node": "pi-node",
                         "opt/pepi-node/bin/pi": "never"}
                if image == "axios":
                    tools["usr/bin/node"] = "task-node"
                for relative, name in tools.items():
                    path = fake / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(f"#!/bin/sh\necho {name}\n")
                    path.chmod(0o755)
                resolved = ":".join(str(fake) + part for part in template.replace("$PATH", task_path).split(":"))
                self.assertEqual(shutil.which("pi", path=resolved), str(fake / "opt/pepi-eval/bin/pi"))
                expected = "usr/bin/node" if image == "axios" else "opt/pepi-node/bin/node"
                self.assertEqual(shutil.which("node", path=resolved), str(fake / expected))

    def test_pi_wrapper_runs_pi_on_its_own_node_with_arguments_intact(self):
        command = self.task_run("/opt/pepi-eval/bin/pi")
        printf = re.search(r"printf '([^']*)' > /opt/pepi-eval/bin/pi", command).group(1)
        wrapper = self.root / "pi"
        subprocess.run(["sh", "-c", f"printf '{printf}' > \"$1\"", "sh", str(wrapper)], check=True)
        self.assertEqual(wrapper.read_text(),
                         '#!/bin/sh\nexec /opt/pepi-node/bin/node /opt/pi/node_modules/.bin/pi "$@"\n')
        node = self.root / "node"
        node.write_text('#!/bin/sh\nfor arg in "$@"; do printf "[%s]" "$arg"; done\n')
        node.chmod(0o755)
        local = self.root / "pi-local"
        local.write_text(wrapper.read_text().replace("/opt/pepi-node/bin/node", str(node)))
        local.chmod(0o755)
        result = subprocess.run([str(local), "--print", "two words"], capture_output=True, check=True)
        self.assertEqual(result.stdout, b"[/opt/pi/node_modules/.bin/pi][--print][two words]")

    def cache_guard(self, **env):
        command = self.task_run('for cache in "${CARGO_HOME:-}" "${GEM_HOME:-}"')
        loop = re.search(r'for cache in .*?done(?=\s+&& chown -R 10101:10101 /opt/pepi-agent)', command).group(0)
        loop = loop.replace('chown -R 10101:10101 "$cache"', 'echo "chown $cache"')
        clean = {k: v for k, v in os.environ.items() if k not in ("CARGO_HOME", "GEM_HOME")}
        return subprocess.run(["sh", "-c", loop], env={**clean, **env}, capture_output=True, text=True)

    def test_cache_handover_refuses_directories_holding_the_controller_path_or_pi(self):
        cargo, gems = self.root / "cargo", self.root / "bundle"
        cargo.mkdir()
        gems.mkdir()
        result = self.cache_guard(CARGO_HOME=str(cargo), GEM_HOME=str(gems) + "/")
        self.assertEqual((result.returncode, result.stdout), (0, f"chown {cargo}\nchown {gems}/\n"))
        self.assertEqual(self.cache_guard(CARGO_HOME=str(self.root / "missing")).stdout, "")
        self.assertEqual(self.cache_guard().stdout, "")  # Python images set neither
        controller = CONTROLLER_ENV[1].split("=", 1)[1].split(":")
        for cache in ("/", "/usr", "/usr/", "/usr/bin", "/bin"):
            with self.subTest(cache=cache):
                result = self.cache_guard(CARGO_HOME=cache)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn("refusing to give", result.stderr)
        kept = re.search(r"for kept in ([^;]*);", self.task_run("for kept in")).group(1).split()
        self.assertTrue(set(controller) <= set(kept))
        self.assertTrue({"/opt/pi", "/opt/pepi-node", "/opt/pepi-eval", "/opt/pepi-mcp"} <= set(kept))

    def test_pi_node_does_not_shadow_the_task_toolchain(self):
        self.assertNotIn("/usr/local/ /usr/local/", self.task)
        self.assertIn("COPY --from=agent /usr/local/ /opt/pepi-node/", self.task)
        self.assertIn("ENV PATH=/opt/pepi-eval/bin:$PATH:/opt/pepi-node/bin", self.task)
        self.assertIn("exec /opt/pepi-node/bin/node /opt/pi/node_modules/.bin/pi", self.task)
        self.assertIn("rm -f /usr/local/bin/yarn /usr/local/bin/yarnpkg", self.agent)

    def test_layer_needs_apt_and_hands_toolchain_caches_to_the_agent_uid(self):
        self.assertIn("command -v apt-get >/dev/null ||", self.task)
        self.assertIn('for cache in "${CARGO_HOME:-}" "${GEM_HOME:-}"', self.task)
        self.assertIn('chown -R 10101:10101 "$cache"', self.task)
        self.assertIn("useradd --uid 10101", self.task)
        self.assertNotIn("conda", self.task)


class McpShebangTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("node") is None:
            self.skipTest("node is not installed")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def pin(self, files):
        for name, data in files.items():
            (self.root / name).write_bytes(data)
            (self.root / name).chmod(0o755)
        script = ("import { pinNodeShebang, PI_NODE } from " + json.dumps(INSTALL_MCP.as_uri()) + ";"
                  "const files = JSON.parse(process.argv[1]);"
                  "process.stdout.write(JSON.stringify({node: PI_NODE, "
                  "pinned: files.map((f) => pinNodeShebang(f))}));")
        result = subprocess.run(["node", "--input-type=module", "-e", script,
                                 json.dumps([str(self.root / name) for name in files])],
                                capture_output=True, check=True, timeout=30)
        return json.loads(result.stdout)

    def test_env_node_servers_are_pinned_to_pi_node_and_others_left_alone(self):
        files = {"plain": b"#!/usr/bin/env node\nconsole.log(1)\n",
                 "flags": b"#!/usr/bin/env node --no-warnings\r\nrun()\n",
                 "split": b"#!/usr/bin/env -S node --flag\nrun()\n",
                 "nodejs": b"#!/usr/bin/env nodejs\nrun()\n",
                 "shell": b"#!/bin/sh\nexec node x\n",
                 "binary": b"\x7fELF\x00\xff"}
        result = self.pin(files)
        self.assertEqual(result, {"node": PI_NODE, "pinned": [True, True, False, False, False, False]})
        self.assertEqual((self.root / "plain").read_bytes(), b"#!" + PI_NODE.encode() + b"\nconsole.log(1)\n")
        self.assertEqual((self.root / "flags").read_bytes(),
                         b"#!" + PI_NODE.encode() + b" --no-warnings\r\nrun()\n")
        for name in ("split", "nodejs", "shell", "binary"):
            self.assertEqual((self.root / name).read_bytes(), files[name])
        self.assertEqual((self.root / "plain").stat().st_mode & 0o777, 0o755)

    def test_importing_the_installer_does_not_provision(self):
        # main() (npm ci) runs only as `node install-mcp.mjs`; the Dockerfile still runs it that way.
        self.assertEqual(self.pin({}), {"node": PI_NODE, "pinned": []})
        agent = DOCKERFILE.read_text().partition("FROM ${TASK_IMAGE}")[0]
        self.assertIn("RUN node /opt/pepi-eval/install-mcp.mjs", agent)
        self.assertIn("COPY --from=agent /usr/local/ /opt/pepi-node/", DOCKERFILE.read_text())


if __name__ == "__main__":
    unittest.main()
