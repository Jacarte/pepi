"""Consented host skills: filtering, hashing, staging, runner delivery, reporting and the script flow.

Synthetic HOME and skills only; the live ~/.agents/skills and ~/.pi/agent/skills are never read.
"""
import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evals.swebench import __main__ as cli
from evals.swebench import narrate
from evals.swebench.common import read_json, write_json
from evals.swebench.host_auth import run_with_host_auth
from evals.swebench.host_skills import (FORBIDDEN_REASON, HOST_SKILLS_SHA256_ENV, MAX_SKILL_DEPTH, MAX_SKILLS,
                                        collect_host_skills, load_staged_skills, skills_archive, skills_sha256,
                                        stage_skills)
from evals.swebench.prepare import prepare_run
from evals.swebench.reporting import COMPARABLE_PROTOCOL_KEYS, configuration_differences
from evals.swebench.runner import CONTROLLER_ENV, SKILLS_DIR, run_tasks
from evals.swebench.tests import test_host_mcp
from evals.swebench.tests.test_ci import ENV, RecordingDocker, write_settings
from evals.swebench.tests.test_cli_script import ScriptHarness
from evals.swebench.tests.test_narrate import summary
from evals.swebench.tests.test_profiles import oauth, prepare, repository
from evals.swebench.tests.test_runner import TASK, VERSIONS

BODY = "SYNTHETIC-SKILL-BODY-0123456789"  # skill contents must never be printed
CREDENTIAL = "SYNTHETIC-SKILL-CREDENTIAL-0123456789"


def write_skill(root: Path, name: str, files: dict | None = None) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    files = {"SKILL.md": f"---\nname: {name}\ndescription: test skill\n---\n{BODY}\n"} if files is None else files
    for relative, content in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
    return directory


def skill_md(name="x"):
    return f"---\nname: {name}\ndescription: d\n---\n{BODY}\n"


class Home:
    """A synthetic HOME with Pi's two user skill sources."""

    def __init__(self, base: Path):
        self.home = base / "home"
        self.agent = self.home / ".pi/agent/skills"
        self.global_ = self.home / ".agents/skills"
        self.agent.mkdir(parents=True)
        self.global_.mkdir(parents=True)


class HostSkillsFilterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.sources = Home(self.base)

    def collect(self, values=()):
        return collect_host_skills(self.sources.home, values)

    def test_pi_order_first_name_wins_even_when_the_first_is_rejected(self):
        write_skill(self.sources.agent, "shared", {"SKILL.md": skill_md("agent-dir")})
        write_skill(self.sources.global_, "shared", {"SKILL.md": skill_md("global")})
        write_skill(self.sources.agent, "blocked", {"SKILL.md": skill_md(), "x.pem": "k"})
        write_skill(self.sources.global_, "blocked")
        write_skill(self.sources.global_, "only-global")
        skills, dropped = self.collect()
        self.assertEqual([skill.name for skill in skills], ["shared", "only-global"])
        self.assertIn(b"agent-dir", skills[0].files[0][1])
        self.assertEqual(dropped, [
            ("blocked", FORBIDDEN_REASON),
            ("blocked", "duplicate name; ~/.pi/agent/skills comes first"),
            ("shared", "duplicate name; ~/.pi/agent/skills comes first")])

    def test_each_rule_rejects_the_whole_skill_with_a_names_only_reason(self):
        root = self.sources.global_
        credential_like = FORBIDDEN_REASON
        cases = {
            "env": ({".env": "A=1"}, credential_like), "env-local": ({".env.local": "A=1"}, credential_like),
            "auth": ({"auth.json": "{}"}, credential_like), "mcp": ({"nested/mcp.json": "{}"}, credential_like),
            "pem": ({"cert.pem": "x"}, credential_like), "key": ({"server.key": "x"}, credential_like),
            "rsa": ({"id_rsa.pub": "x"}, credential_like), "git": ({".git/HEAD": "ref"}, credential_like),
            "git-file": ({".git": "gitdir: x"}, credential_like), "env-upper": ({".ENV": "A=1"}, credential_like),
            "ed25519": ({"keys/id_ed25519": "x"}, credential_like), "ecdsa": ({"id_ecdsa": "x"}, credential_like),
            "dsa": ({"id_dsa.pub": "x"}, credential_like), "netrc": ({".netrc": "x"}, credential_like),
            "npmrc": ({".npmrc": "x"}, credential_like), "pypirc": ({".pypirc": "x"}, credential_like),
            "credentials": ({"Credentials.json": "{}"}, credential_like),
            "p12": ({"client.P12": "x"}, credential_like), "pfx": ({"client.pfx": "x"}, credential_like),
            "keystore": ({"app.keystore": "x"}, credential_like), "jks": ({"trust.jks": "x"}, credential_like),
            "private-key": ({"notes.md": "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n"},
                            "a file matches a secret/token pattern"),
            "json-key": ({"cfg.json": '{"apiKey": "abcdefgh1234"}'}, "a file matches a secret/token pattern"),
            "pgp-key": ({"notes.md": "-----BEGIN PGP PRIVATE KEY BLOCK-----\nabc\n"}, "a file matches a secret/token pattern"),
            "aws-key": ({"cfg.md": "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG\n"}, "a file matches a secret/token pattern"),
            "special-password": ({"cfg.json": '{"password": "p@ssw0rd!23"}'}, "a file matches a secret/token pattern"),
            "refresh": ({"cfg.json": '{"refresh_token": "abcdefgh1234"}'}, "a file matches a secret/token pattern"),
            "gcp": ({"sa.json": '{"private_key": "MIIEvQIBADANBg"}'}, "a file matches a secret/token pattern"),
            "password": ({"notes.md": "password = hunter2hunter2"}, "a file matches a secret/token pattern"),
            "deep": ({"/".join(["d"] * (MAX_SKILL_DEPTH + 1)) + "/f.md": "x"},
                     f"nested deeper than {MAX_SKILL_DEPTH} directories"),
            "missing": ({"README.md": "x"}, "SKILL.md missing"),
            "nested-only": ({"sub/SKILL.md": skill_md()}, "SKILL.md missing"),
            "big-file": ({"big.bin": b"0" * (1048576 + 1)}, "a file is larger than 1 MiB"),
            "big-skill": ({f"part{i}.bin": b"0" * 1048576 for i in range(6)}, "larger than 5 MiB"),
            "many": ({f"f{i}.txt": "x" for i in range(201)}, "more than 200 files"),
            "token": ({"notes.md": "token: abc123"}, "a file matches a secret/token pattern"),
            "openai": ({"notes.md": "use sk-abcdefghijkl"}, "a file matches a secret/token pattern"),
        }
        for name, (files, _) in cases.items():
            write_skill(root, name, {"SKILL.md": skill_md(), **files} if name not in ("missing", "nested-only")
                        else files)
        write_skill(root, "linked")
        (root / "linked/elsewhere.md").symlink_to(self.base / "outside.md")
        write_skill(root, "fifo")
        os.mkfifo(root / "fifo/pipe")
        os.symlink(root / "fifo", root / "alias")
        write_skill(root, "bad name")
        write_skill(root, "bad\nname")
        write_skill(root, ".hidden")
        (root / "README.md").write_text("x")
        write_skill(root, "ok")
        skills, dropped = self.collect()
        self.assertEqual([skill.name for skill in skills], ["ok"])
        expected = {name: reason for name, (_, reason) in cases.items()}
        expected |= {"linked": "contains a symlink", "fifo": "contains a special file",
                     "alias": "symlink; not followed",
                     ".hidden": "hidden entry; not a skill", "README.md": "not a skill directory"}
        reasons = dict(dropped)
        self.assertEqual({name: reasons.get(name) for name in expected}, expected)
        # "bad name" and "bad\nname" are not printed: both show as placeholders.
        self.assertEqual([reason for name, reason in dropped if name.startswith("<entry #")], ["invalid skill name"] * 2)
        self.assertNotIn(BODY, json.dumps(dropped))

    def test_credential_prose_and_placeholders_are_not_rejected(self):
        prose = ("Follow the password policy: rotate quarterly.\npassword: ask\nPass the apiKey option.\n"
                 'Set "apiKey": "<your key>" and the private_key field; a BEGIN CERTIFICATE block is fine.\n')
        write_skill(self.sources.global_, "prose", {"SKILL.md": skill_md() + prose})
        skills, dropped = self.collect()
        self.assertEqual(([skill.name for skill in skills], dropped), (["prose"], []))

    def test_symlinked_or_swapped_subdirectory_and_hard_links_are_refused(self):
        root = self.sources.global_
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "id.txt").write_text("x")
        write_skill(root, "linked-dir")
        (root / "linked-dir/refs").symlink_to(outside, target_is_directory=True)
        write_skill(root, "hard")
        os.link(outside / "id.txt", root / "hard/notes.md")
        # Race: `refs` is a directory when inspected, then a symlink when opened.
        write_skill(root, "swapped", {"SKILL.md": skill_md(), "refs/a.md": "a"})
        real_stat, swapped = os.stat, (root / "swapped").stat().st_ino
        swaps = []

        def swap_after_stat(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            directory = kwargs.get("dir_fd")
            if path == "refs" and directory is not None and os.fstat(directory).st_ino == swapped and not swaps:
                shutil.rmtree(root / "swapped/refs")
                (root / "swapped/refs").symlink_to(outside, target_is_directory=True)
                swaps.append(stat.S_ISDIR(info.st_mode))
            return info

        with patch("os.stat", swap_after_stat):
            skills, dropped = self.collect()
        self.assertEqual((skills, swaps), ([], [True]))  # the swap happened after a directory was seen
        self.assertEqual(dict(dropped), {"hard": "contains a hard-linked file", "linked-dir": "contains a symlink",
                                         "swapped": "contains a symlink"})

    def test_credential_values_of_the_run_are_refused_and_short_values_ignored(self):
        write_skill(self.sources.global_, "leaky", {"SKILL.md": skill_md() + CREDENTIAL})
        write_skill(self.sources.global_, "short", {"SKILL.md": skill_md() + "abc"})
        skills, dropped = self.collect([CREDENTIAL.encode(), b"abc"])
        self.assertEqual([skill.name for skill in skills], ["short"])
        self.assertEqual(dropped, [("leaky", "a file contains a credential value of this run")])

    def test_symlinked_source_is_not_followed_and_skill_count_is_capped(self):
        shutil.rmtree(self.sources.global_)
        target = self.base / "dotfiles-skills"
        write_skill(target, "via-link")
        self.sources.global_.symlink_to(target)
        for index in range(MAX_SKILLS + 1):
            write_skill(self.sources.agent, f"s{index:02d}")
        skills, dropped = self.collect()
        self.assertEqual(len(skills), MAX_SKILLS)
        self.assertIn(("s50", f"more than {MAX_SKILLS} skills"), dropped)
        self.assertIn(("~/.agents/skills", "skills directory is a symlink; not followed"), dropped)

    def test_digest_is_deterministic_and_covers_bytes_paths_and_executable_bits(self):
        first, second = Home(self.base / "a"), Home(self.base / "b")
        write_skill(first.global_, "tool", {"SKILL.md": skill_md(), "scripts/run.sh": "#!/bin/sh\n"})
        write_skill(second.global_, "tool", {"scripts/run.sh": "#!/bin/sh\n", "SKILL.md": skill_md()})
        skills = [collect_host_skills(home.home)[0] for home in (first, second)]
        self.assertEqual(skills_sha256(skills[0]), skills_sha256(skills[1]))
        self.assertEqual(skills[0][0].record(), {"name": "tool", "sha256": skills[0][0].sha256, "files": 2})
        self.assertEqual(skills[0][0].scripts, 1)
        digest = skills_sha256(skills[0])
        (first.global_ / "tool/scripts/run.sh").chmod(0o755)
        self.assertNotEqual(skills_sha256(collect_host_skills(first.home)[0]), digest)
        (second.global_ / "tool/scripts/run.sh").write_text("#!/bin/sh\necho\n")
        self.assertNotEqual(skills_sha256(collect_host_skills(second.home)[0]), digest)
        (second.global_ / "tool/scripts/run.sh").rename(second.global_ / "tool/scripts/other.sh")
        self.assertNotEqual(skills_sha256(collect_host_skills(second.home)[0]), digest)


class StagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.sources = Home(self.base)
        write_skill(self.sources.global_, "alpha", {"SKILL.md": skill_md(), "scripts/go.sh": "#!/bin/sh\n",
                                                    "refs/deep/notes.md": "n"})
        (self.sources.global_ / "alpha/scripts/go.sh").chmod(0o755)
        self.capture = self.base / "capture.json"
        self.inspector = [sys.executable, "-c", (
            "import json, os, pathlib, stat, sys; a = sys.argv[2:];"
            "d = pathlib.Path(a[a.index('--skills-dir') + 1]) if '--skills-dir' in a else None;"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({'args': a, 'directory': str(d) if d else None,"
            " 'digest_env': os.environ.get('PEPI_EVAL_HOST_SKILLS_SHA256'),"
            " 'modes': {str(p.relative_to(d)): stat.S_IMODE(p.lstat().st_mode) for p in d.rglob('*')} if d else {},"
            " 'dir_mode': stat.S_IMODE(d.stat().st_mode) if d else None,"
            " 'skill_md': (d / 'alpha/SKILL.md').read_text() if d else None}))"),
            str(self.capture)]

    def stage(self, env=None, **flags):
        with patch.dict(os.environ, {"HOME": str(self.sources.home), **(env or {})}):
            status = run_with_host_auth(self.base / "unused-run", self.inspector, auth=False, skills=True, **flags)
        return status, json.loads(self.capture.read_text()) if self.capture.exists() else None

    def test_skills_only_stage_privately_without_a_secrets_dir_then_clean_up(self):
        approved = skills_sha256(collect_host_skills(self.sources.home)[0])
        status, data = self.stage(env={HOST_SKILLS_SHA256_ENV: approved}, approved_skills_sha256=approved)
        self.assertEqual(status, 0)
        self.assertNotIn("--secrets-dir", data["args"])
        self.assertEqual(data["dir_mode"], 0o700)
        self.assertEqual(data["modes"], {"alpha": 0o700, "alpha/SKILL.md": 0o600, "alpha/scripts": 0o700,
                                         "alpha/scripts/go.sh": 0o700, "alpha/refs": 0o700,
                                         "alpha/refs/deep": 0o700, "alpha/refs/deep/notes.md": 0o600})
        self.assertEqual(data["skill_md"], skill_md())
        self.assertIsNone(data["digest_env"])  # the runner child never inherits the pin
        self.assertFalse(Path(data["directory"]).exists())

    def test_source_edited_after_approval_exits_2_and_stages_nothing(self):
        approved = skills_sha256(collect_host_skills(self.sources.home)[0])
        (self.sources.global_ / "alpha/refs/deep/notes.md").write_text("edited")
        before = set(Path("/tmp").glob("pepi-eval-skills-*"))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status, data = self.stage(approved_skills_sha256=approved)
        self.assertEqual(status, 2)
        self.assertIn("host skills changed since approval; re-run", stderr.getvalue())
        self.assertIsNone(data)
        self.assertEqual(before, set(Path("/tmp").glob("pepi-eval-skills-*")))

    def test_skill_holding_the_gateway_url_fails_at_staging_naming_the_skill(self):
        gateway = "https://gateway.internal.example/v1"
        (self.sources.global_ / "alpha/refs/deep/notes.md").write_text(f"Use {gateway} for calls")
        approved = skills_sha256(collect_host_skills(self.sources.home)[0])  # the preview does not know the URL
        before = set(Path("/tmp").glob("pepi-eval-skills-*"))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status, data = self.stage(env={"LITELLM_BASE_URL": gateway}, approved_skills_sha256=approved)
        self.assertEqual((status, data), (2, None))
        self.assertIn("host skill alpha contains a credential value of this run", stderr.getvalue())
        self.assertNotIn(gateway, stderr.getvalue())
        self.assertEqual(before, set(Path("/tmp").glob("pepi-eval-skills-*")))

    def test_no_usable_skill_runs_without_a_skills_dir(self):
        (self.sources.global_ / "alpha/.env").write_text("A=1")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status, data = self.stage()
        self.assertEqual(status, 0)
        self.assertIsNone(data["directory"])
        self.assertIn("continuing without host skills", stderr.getvalue())

    def test_staged_directory_is_revalidated(self):
        skills, _ = collect_host_skills(self.sources.home)
        directory = self.base / "staged"
        directory.mkdir(mode=0o700)
        stage_skills(skills, directory)
        self.assertEqual(load_staged_skills(directory), skills)
        with self.assertRaisesRegex(ValueError, "rejected: a file contains a credential value"):
            load_staged_skills(directory, [b"#!/bin/sh"])
        (directory / "stray.md").write_text("x")
        with self.assertRaisesRegex(ValueError, "Unexpected entry"):
            load_staged_skills(directory)
        (directory / "stray.md").unlink()
        os.symlink(self.base, directory / "alpha/refs/link")
        with self.assertRaisesRegex(ValueError, "Skill alpha rejected: contains a symlink"):
            load_staged_skills(directory)
        (directory / "alpha/refs/link").unlink()
        directory.chmod(0o750)
        with self.assertRaisesRegex(ValueError, "chmod 700"):
            load_staged_skills(directory)

    def test_archive_is_deterministic_root_owned_and_readable_by_the_agent(self):
        skills, _ = collect_host_skills(self.sources.home)
        data = skills_archive(skills)
        self.assertEqual(data, skills_archive(skills))
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            members = {member.name: member for member in archive.getmembers()}
            self.assertEqual({name: (member.type, oct(member.mode)) for name, member in members.items()}, {
                "alpha": (tarfile.DIRTYPE, "0o755"), "alpha/refs": (tarfile.DIRTYPE, "0o755"),
                "alpha/refs/deep": (tarfile.DIRTYPE, "0o755"), "alpha/scripts": (tarfile.DIRTYPE, "0o755"),
                "alpha/SKILL.md": (tarfile.REGTYPE, "0o644"), "alpha/refs/deep/notes.md": (tarfile.REGTYPE, "0o644"),
                "alpha/scripts/go.sh": (tarfile.REGTYPE, "0o755")})
            self.assertTrue(all(member.uid == 0 and member.gid == 0 and member.mtime == 0
                                for member in members.values()))
            self.assertEqual(archive.extractfile("alpha/SKILL.md").read().decode(), skill_md())


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        root = self.base / "repo"
        write_settings(root)
        self.run = self.base / "run-1"
        prepare_run(root, self.run, [TASK], [TASK["instance_id"]], VERSIONS, "node:22")
        write_json(self.run / "builds.json", {TASK["instance_id"]: {"image_id": "sha256:frozen"}})
        self.sources = Home(self.base)

    def staged(self, files=None):
        write_skill(self.sources.global_, "alpha", files)
        skills, _ = collect_host_skills(self.sources.home)
        directory = self.base / "staged"
        directory.mkdir(mode=0o700)
        stage_skills(skills, directory)
        return directory, skills

    def execute(self, docker, skills_dir):
        with patch.dict(os.environ, ENV), patch("evals.swebench.runner.Docker", return_value=docker):
            return run_tasks(self.run, 1, "eval", "2", "4g", None, (), skills_dir)

    def test_skills_go_to_a_root_owned_tmpfs_and_execution_records_names_and_hashes(self):
        directory, skills = self.staged()
        docker = RecordingDocker()
        self.assertEqual(self.execute(docker, directory), 0)
        create = next(command for command in docker.commands if command[0] == "create")
        mount = create[create.index(next(a for a in create if a.startswith(SKILLS_DIR + ":"))) - 1:][:2]
        self.assertEqual(mount[0], "--tmpfs")
        self.assertRegex(mount[1], r"^/opt/pepi-agent/skills:rw,exec,nosuid,nodev,size=\d+k,mode=0755$")
        extract = next(command for command in docker.commands if "tar" in command)
        self.assertEqual(extract[:2], ("exec", "-i"))
        self.assertNotIn("--user", extract)  # root extracts: the agent UID cannot write
        self.assertEqual(extract[2:4], CONTROLLER_ENV)
        self.assertEqual(extract[5:], ("tar", "-x", "-f", "-", "-C", SKILLS_DIR))
        self.assertIn(skills_archive(skills), [kwargs.get("input") for kwargs in docker.kwargs])
        self.assertFalse(any(BODY in arg for command in docker.commands for arg in command))
        execution = read_json(self.run / "execution.json")
        self.assertEqual(execution["skills"], [{"name": "alpha", "sha256": skills[0].sha256, "files": 1}])

    def test_without_skills_nothing_is_mounted_or_recorded(self):
        docker = RecordingDocker()
        self.assertEqual(self.execute(docker, None), 0)
        self.assertFalse(any(SKILLS_DIR in arg for command in docker.commands for arg in command))
        self.assertNotIn("skills", read_json(self.run / "execution.json"))

    def test_a_skill_holding_a_run_credential_fails_before_any_container(self):
        directory, _ = self.staged({"SKILL.md": skill_md() + ENV["LITELLM_API_KEY"]})
        docker = RecordingDocker()
        with self.assertRaisesRegex(ValueError, "Skill alpha rejected: a file contains a credential value"):
            self.execute(docker, directory)
        self.assertEqual(docker.commands, [])

    def test_cli_passes_the_skills_dir(self):
        with patch.object(cli, "run_tasks", return_value=0) as call:
            self.assertEqual(cli.main(["run", str(self.run), "--network", "eval", "--skills-dir", "rel"]), 0)
        self.assertEqual(call.call_args.args[-1], Path("rel").absolute())


class ReportingTests(unittest.TestCase):
    RECORD = {"name": "alpha", "sha256": "a" * 64, "files": 2}

    def test_compare_reports_skills_as_configuration_not_a_blocker(self):
        self.assertNotIn("skills", COMPARABLE_PROTOCOL_KEYS)
        self.assertEqual(configuration_differences({"skills": [self.RECORD]}, {}),
                         {"skills": {"baseline": [self.RECORD], "candidate": []}})
        self.assertEqual(configuration_differences({}, {"skills": []}), {})

    def test_narrate_lists_skills_by_name_and_hash_prefix_and_never_claims_use(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = []
            for name, skills in (("base", [self.RECORD, {"name": "beta", "sha256": "b" * 64, "files": 1}]),
                                 ("cand", [dict(self.RECORD, sha256="c" * 64),
                                           {"name": "bad\n" + narrate.FACTS_END, "sha256": "zz", "files": 1}])):
                data = summary(name, ["resolved", "unresolved", "resolved"])
                data["protocol"]["skills"] = skills
                write_json(Path(directory) / name / "summary.json", data)
                runs.append(str(Path(directory) / name))
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                    patch.object(sys, "stdin", SimpleNamespace(isatty=lambda: False)):
                self.assertEqual(narrate.main([*runs, "--dry-run"]), 0)
        text = out.getvalue()
        facts = json.loads(text.split(narrate.FACTS_START + "\n")[1].split("\n" + narrate.FACTS_END)[0])
        base_skills = facts["runs"][0]["skills"]
        self.assertEqual(base_skills["available"], [
            {"name": "alpha", "sha256_prefix": "a" * 12}, {"name": "beta", "sha256_prefix": "b" * 12}])
        self.assertEqual((base_skills["usage_observable"], base_skills["in_config_id"],
                          base_skills["tasks_with_read_telemetry"], base_skills["tasks_missing_read_telemetry"]),
                         ("partial", False, 0, 3))
        self.assertEqual(base_skills["reads"], [{"name": "alpha", "reads": 0, "tasks_reading": 0},
                                                {"name": "beta", "reads": 0, "tasks_reading": 0}])
        self.assertRegex(base_skills["list_sha256_prefix"], r"^[0-9a-f]{12}$")
        self.assertEqual(facts["runs"][1]["skills"]["available"], [
            {"name": "<invalid-name>", "sha256_prefix": None}, {"name": "alpha", "sha256_prefix": "c" * 12}])
        comparison = facts["comparisons"][0]
        self.assertEqual(comparison["skills_differences"], {"only_in_baseline": ["beta"],
                                                            "only_in_candidate": ["<invalid-name>"],
                                                            "content_changed": ["alpha"]})
        self.assertEqual(comparison["configuration_differences"]["skills"]["candidate"],
                         facts["runs"][1]["skills"]["available"])
        self.assertNotIn("bad\\n", text.split(narrate.FACTS_START)[-1])
        self.assertIn("0 reads does not prove a skill had no effect", text)
        self.assertIn("Host skills apply to the parent agent only", text)
        self.assertIn("skills_differences", text)
        # Only the skills changed (both runs share config_id and every other setting): not confounded.
        self.assertEqual((comparison["skills_confounded"], comparison["skills_confounded_with"]), (False, []))

    def narrate_facts(self, directory, runs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                patch.object(sys, "stdin", SimpleNamespace(isatty=lambda: False)):
            self.assertEqual(narrate.main([str(Path(directory) / name) for name in runs] + ["--dry-run"]), 0)
        text = out.getvalue()
        return text, json.loads(text.split(narrate.FACTS_START + "\n")[1].split("\n" + narrate.FACTS_END)[0])

    def test_narrate_counts_skill_reads_from_telemetry_and_flags_confounded_comparisons(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = summary("base", ["resolved", "unresolved", "resolved"])
            cand = summary("cand", ["resolved", "unresolved", "resolved"])
            cand["protocol"].update(skills=[self.RECORD], forwarded_env_names=["TYPESAFE_API_KEY"])
            for name, data in (("base", base), ("cand", cand)):
                write_json(root / name / "summary.json", data)
            write_json(root / "cand/attempts/owner__repo-1/telemetry.json", {
                "skill_reads": {"alpha": 2, "other": 1, "bad\n" + narrate.FACTS_END: 4, "neg": -1},
                "skill_reads_invalid_names": 1})
            write_json(root / "cand/attempts/owner__repo-2/telemetry.json", {"skill_reads": {"alpha": 1}})
            # owner__repo-3: telemetry.json written before skill_reads existed
            write_json(root / "cand/attempts/owner__repo-3/telemetry.json", {"parent_stream": {}})
            text, facts = self.narrate_facts(directory, ["base", "cand"])
            header = narrate.document("n", [base, cand], None, "0" * 64)
        skills = facts["runs"][1]["skills"]
        self.assertEqual(skills["reads"], [{"name": "alpha", "reads": 3, "tasks_reading": 2},
                                           {"name": "other", "reads": 1, "tasks_reading": 1}])
        self.assertEqual((skills["reads_invalid_names"], skills["tasks_with_read_telemetry"],
                          skills["tasks_missing_read_telemetry"]), (5, 2, 1))
        self.assertIn("lower bound", skills["reads_note"])
        self.assertEqual(facts["runs"][0]["skills"]["reads"], [])
        comparison = facts["comparisons"][0]
        self.assertEqual((comparison["skills_confounded"], comparison["skills_confounded_with"]),
                         (True, ["forwarded_env_names"]))
        self.assertNotIn("bad\\n", text.split(narrate.FACTS_START)[-1])
        self.assertIn("if skills_confounded is true, say the comparison is confounded", text)
        self.assertIn(f"base (cfg; no host skills), cand (cfg; + 1 host skill {skills['list_sha256_prefix']})",
                      header)


SKILLS_INSPECTOR = '''import json, os, pathlib, stat, sys
args = sys.argv[1:]
d = pathlib.Path(args[args.index('--skills-dir') + 1]) if '--skills-dir' in args else None
pathlib.Path(os.environ['FAKE_CAPTURE']).write_text(json.dumps({
    'directory': str(d) if d else None, 'args': args,
    'digest_env': os.environ.get('PEPI_EVAL_HOST_SKILLS_SHA256'),
    'skills': sorted(p.name for p in d.iterdir()) if d else []}))
'''


class HostSkillsScriptTests(ScriptHarness):
    tty = test_host_mcp.HostMcpScriptTests.tty

    def fixture(self, with_skills=True):
        base = Path(self.runs_tmpdir.name)
        prepare(repository(base), base / "host-run", profile_name="codex")
        (self.home / ".pi/agent").mkdir(parents=True)
        (self.home / ".pi/agent/auth.json").write_text(json.dumps(oauth()))
        (self.home / ".pi/agent/auth.json").chmod(0o600)
        self.sources = self.home / ".agents/skills"
        if with_skills:
            write_skill(self.sources, "alpha")
            write_skill(self.sources, "beta", {"SKILL.md": skill_md(), "id_rsa": "x"})
        inspector = Path(self.fake.name) / "inspect.py"
        inspector.write_text(SKILLS_INSPECTOR)
        (Path(self.fake.name) / "venv/bin/python").write_text(
            '#!/bin/sh\n[ "$1" = "-m" ] || exit 0\nprintf \'%s\\n\' "$*" >> "$FAKE_LOG"\n'
            'exec python3 "$FAKE_INSPECTOR" "$@"\n')
        self.capture = Path(self.fake.name) / "capture.json"
        self.python3_log = Path(self.fake.name) / "python3.log"
        python3 = Path(self.fake.name) / "bin/python3"
        python3.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> "{self.python3_log}"\n'
                           f'exec "{shutil.which("python3")}" "$@"\n')
        python3.chmod(0o755)
        return {"PEPI_EVAL_RUN_NAME": "host-run", "PEPI_EVAL_USE_HOST_AUTH": "0", "PEPI_EVAL_USE_HOST_MCP": "0",
                "FAKE_INSPECTOR": str(inspector), "FAKE_CAPTURE": str(self.capture)}

    def captured(self):
        return json.loads(self.capture.read_text())

    def previews(self):
        return self.python3_log.read_text().count("host_skills") if self.python3_log.exists() else 0

    def assert_no_contents(self, *texts):
        self.assertNotIn(BODY, "".join(texts) + (self.log.read_text() if self.log.exists() else ""))

    def test_explicit_opt_in_lists_names_and_copies_only_usable_skills(self):
        env = self.fixture()
        result = self.run_script(["-y", "run"], {**env, "PEPI_EVAL_USE_HOST_SKILLS": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.captured()
        self.assertEqual(data["skills"], ["alpha"])
        self.assertNotIn("--secrets-dir", data["args"])
        self.assertFalse(Path(data["directory"]).exists())
        self.assertIn("copy alpha (1 file, ", result.stderr)
        self.assertIn("drop beta (contains a credential-like file", result.stderr)
        self.assertIn("sent to the model provider", result.stderr)
        self.assertNotIn("Allow temporary host skills copy?", result.stderr)
        self.assert_no_contents(result.stdout, result.stderr)

    def test_secrets_dir_keeps_its_own_argument_and_adds_the_skills_dir(self):
        env = self.fixture()
        secrets = Path(self.fake.name) / "secrets"
        secrets.mkdir(mode=0o700)
        result = self.run_script(["-y", "run"], {**env, "PEPI_EVAL_SECRETS_DIR": str(secrets),
                                                 "PEPI_EVAL_USE_HOST_SKILLS": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.captured()["args"]
        self.assertEqual(args.count("--secrets-dir"), 1)
        self.assertEqual(args[args.index("--secrets-dir") + 1], str(secrets))
        self.assertEqual(self.captured()["skills"], ["alpha"])

    def test_without_consent_nothing_is_read_or_copied(self):
        env = self.fixture()
        for extra in ({}, {"PEPI_EVAL_YES": "1"}, {"PEPI_EVAL_USE_HOST_SKILLS": "0"}):
            with self.subTest(extra=extra):
                self.capture.unlink(missing_ok=True)
                result = self.run_script(["-y", "run"], {**env, **extra})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIsNone(self.captured()["directory"])
                self.assertEqual(self.previews(), 0)
                hint = "host skills not copied (no terminal); set PEPI_EVAL_USE_HOST_SKILLS=1"
                (self.assertNotIn if extra.get("PEPI_EVAL_USE_HOST_SKILLS") == "0" else self.assertIn)(
                    hint, result.stderr)

    def test_bad_values_missing_sources_and_dry_run(self):
        env = self.fixture(with_skills=False)
        for option in ("", "yes", "2"):
            with self.subTest(option=option):
                result = self.run_script(["-y", "run"], {**env, "PEPI_EVAL_USE_HOST_SKILLS": option})
                self.assertEqual(result.returncode, 2)
                self.assertIn("PEPI_EVAL_USE_HOST_SKILLS must be 0 or 1", result.stderr)
        result = self.run_script(["-y", "run"], {**env, "PEPI_EVAL_USE_HOST_SKILLS": "1"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("No host skills directory found", result.stderr)
        result = self.run_script(["-y", "run"], env)  # nothing to propose: silent
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("host skills", result.stderr)
        write_skill(self.sources, "alpha")
        self.capture.unlink(missing_ok=True)
        result = self.run_script(["--dry-run", "-y", "run"], {**env, "PEPI_EVAL_USE_HOST_SKILLS": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dry-run: host skills (", result.stderr)
        self.assertIn("not read; a real run asks before copying them", result.stderr)
        self.assertEqual(self.previews(), 0)
        self.assertFalse(self.capture.exists())

    def test_dotenv_cannot_consent(self):
        env = self.fixture()
        dotenv = self.launch / ".env"
        dotenv.write_text(f"PEPI_EVAL_USE_HOST_SKILLS=1\nPEPI_EVAL_HOST_SKILLS_SHA256={'0' * 64}\n")
        dotenv.chmod(0o600)
        result = self.run_script(["-y", "run"], env)
        self.assertEqual(result.returncode, 0, result.stderr)
        warning = next(line for line in result.stderr.splitlines() if "Ignoring shell-only setting(s)" in line)
        self.assertIn("PEPI_EVAL_USE_HOST_SKILLS", warning)
        self.assertIn("PEPI_EVAL_HOST_SKILLS_SHA256", warning)
        self.assertIsNone(self.captured()["directory"])
        self.assertIn("host skills not copied (no terminal)", result.stderr)

    def test_terminal_consent_defaults_to_no_pins_the_list_and_refuses_drift(self):
        env = self.fixture()
        approved = skills_sha256(collect_host_skills(self.home)[0])
        status, output = self.tty(env, {"Allow temporary host skills copy? [y/N]": "y"})
        self.assertEqual(status, 0, output)
        self.assertEqual(self.captured()["skills"], ["alpha"])
        self.assertIsNone(self.captured()["digest_env"])
        self.assertNotIn(approved, self.python3_log.read_text() + self.log.read_text())  # never argv
        self.assert_no_contents(output)
        for answer in ("", "n"):
            with self.subTest(answer=answer):
                self.capture.unlink()
                status, output = self.tty(env, {"Allow temporary host skills copy? [y/N]": answer})
                self.assertEqual(status, 0, output)
                self.assertIsNone(self.captured()["directory"])

        def edit_then_consent():
            (self.sources / "alpha/SKILL.md").write_text(skill_md("edited"))
            return "y"

        self.capture.unlink()
        status, output = self.tty(env, {"Allow temporary host skills copy? [y/N]": edit_then_consent})
        self.assertEqual(status, 2, output)
        self.assertIn("host skills changed since approval; re-run", output)
        self.assertFalse(self.capture.exists())


if __name__ == "__main__":
    unittest.main()
