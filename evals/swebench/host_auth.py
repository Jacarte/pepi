"""Explicit-consent CLI adapter: filtered host auth/mcp.json and skills live only for one runner child."""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .common import read_manifest
from .host_skills import (HOST_SKILLS_SHA256_ENV, collect_host_skills, credential_values, skill_holding,
                          skills_sha256, stage_skills)
from .mcp import McpServerRejected, check_mcp_server, native_mcp_available, validate_mcp_secret
from .runner import Secrets, frozen_mcp_policy, frozen_provider_plan

HOST_AUTH = ".pi/agent/auth.json"
HOST_MCP = ".pi/agent/mcp.json"
# Digest of the filtered mcp.json the user approved from the preview list; set by the shell only.
HOST_MCP_SHA256_ENV = "PEPI_EVAL_HOST_MCP_SHA256"
MAX_SOURCE_BYTES = 1048576
# Printed names (servers, env/header/oauth keys, top-level keys); anything else is shown as a placeholder.
PRINTABLE_NAME = r"[A-Za-z0-9_.-]{1,128}"
PRINTABLE_HOST = r"[A-Za-z0-9.:-]{1,253}"


def _read_host_file(source: Path, label: str, forbidden_mode: int, requirement: str) -> tuple[bytes, int]:
    """Read a regular file owned by this user through a no-follow FD, bounded in size.

    Returns (content, permission bits of the opened file)."""
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & forbidden_mode or not info.st_mode & stat.S_IRUSR):
            raise ValueError(f"{label} must be {requirement}")
        content = stream.read(MAX_SOURCE_BYTES + 1)
    if len(content) > MAX_SOURCE_BYTES:
        raise ValueError(f"{label} is too large")
    return content, stat.S_IMODE(info.st_mode)


def required_host_auth(run: Path, source: Path) -> bytes:
    """Validate the frozen contract before opening a private, no-follow source FD."""
    plan = frozen_provider_plan(run)
    version = read_manifest(run)["versions"]["pi"]
    content, _ = _read_host_file(source, "Host auth", 0o077, "a readable private regular file owned by this user")
    auth = json.loads(content)
    if not isinstance(auth, dict):
        raise ValueError("Host auth must be an object")
    # Explicit host opt-in requires every planned provider in the source, not an env fallback.
    if any(name not in auth for name in plan["required_providers"]):
        raise ValueError("Host auth is missing a frozen provider")
    content = json.dumps({name: auth[name] for name in plan["required_providers"]}).encode()
    Secrets({"auth.json": content}).validate(plan, version)
    return content


def _printable(name, placeholder: str, pattern: str = PRINTABLE_NAME) -> str:
    return name if isinstance(name, str) and re.fullmatch(pattern, name) else placeholder


def filter_host_mcp(content: bytes, policy: dict, pi_version: str) -> tuple[bytes | None, list[dict], list[tuple[str, str]], list[str]]:
    """Keep only the servers this run can launch, using the runtime's own rules (mcp.py).

    Returns (filtered mcp.json or None when nothing survives, kept summaries,
    dropped (name, reason) pairs, ignored top-level keys). Summaries and reasons hold
    names only, never values. The caller checks that native MCP is enabled.
    """
    try:
        config = json.loads(content)
    except (ValueError, UnicodeError):
        raise ValueError("Host mcp.json must be valid JSON") from None
    if not isinstance(config, dict) or not isinstance(config.get("mcpServers"), dict):
        raise ValueError("Host mcp.json needs a mcpServers object")
    extra = {}
    if isinstance(config.get("autoEnableCodemode"), bool):
        extra["autoEnableCodemode"] = config["autoEnableCodemode"]
    ignored = sorted(_printable(key, "<key>") for key in config if key != "mcpServers" and key not in extra)
    kept, summaries, dropped, accepted = {}, [], [], set()
    for index, (name, server) in enumerate(config["mcpServers"].items(), 1):
        label = _printable(name, f"<server #{index}>")
        # Native Pi uses `enabled: false`; MCP adapters use `disabled: true`. Honor both.
        if isinstance(server, dict) and (server.get("enabled") is False or server.get("disabled") is True):
            dropped.append((label, "disabled in host config"))
            continue
        try:
            check_mcp_server(name, server, policy, accepted)
        except McpServerRejected as error:
            dropped.append((label, error.reason))
            continue
        accepted.add(name.replace("-", "_"))
        kept[name] = server
        summary = {"name": name, "type": "stdio" if "command" in server else "http"}
        if "url" in server:
            summary["host"] = _printable(urlsplit(server["url"]).hostname, "<host>", PRINTABLE_HOST)
        for field in ("env", "headers", "oauth"):
            keys = sorted(_printable(key, "<key>") for key in server.get(field, {}))
            if keys:
                summary[field] = keys
        summaries.append(summary)
    if not kept:
        return None, summaries, dropped, ignored
    filtered = json.dumps({"mcpServers": kept, **extra}).encode()
    validate_mcp_secret(filtered, policy, pi_version)
    return filtered, summaries, dropped, ignored


def host_mcp(run: Path, source: Path) -> tuple[bytes | None, list[dict], list[tuple[str, str]], list[str], bool]:
    """Filter the host mcp.json against the hash-verified MCP policy of `run`.

    Readable-by-others sources are accepted (the copy is private) but reported; writable ones are not.
    """
    policy = frozen_mcp_policy(run)
    version = read_manifest(run)["versions"]["pi"]
    if not policy.get("enabled") or not native_mcp_available(version):
        raise ValueError("Native MCP is disabled for this run (--drop-mcp, legacy profile or Pi < 1.1.0)")
    content, mode = _read_host_file(source, "Host mcp.json", 0o022,
                                    "a regular file owned by this user, not writable by group/others")
    return (*filter_host_mcp(content, policy, version), bool(mode & 0o044))


def host_mcp_sha256(content: bytes | None) -> str:
    """Digest of the filtered mcp.json; no survivor (None) hashes as empty content."""
    return hashlib.sha256(content or b"").hexdigest()


def describe_host_mcp(source: Path, summaries: list[dict], dropped: list[tuple[str, str]],
                      ignored: list[str], shared: bool) -> list[str]:
    lines = [f"pepi-eval: warning: Use {source} for this run? Only servers this run can launch are copied, "
             "to a private temporary directory (mode 700, file 600), then deleted."]
    if shared:
        lines.append(f"pepi-eval: warning: {source} is readable by group/others; consider chmod 600.")
    for summary in summaries:
        details = [summary["type"]] + ([f"host {summary['host']}"] if "host" in summary else [])
        details += [f"{field} keys: {', '.join(summary[field])}" for field in ("env", "headers", "oauth")
                    if field in summary]
        lines.append(f"pepi-eval:   copy {summary['name']} ({'; '.join(details)})")
    lines += [f"pepi-eval:   drop {name} ({reason})" for name, reason in dropped]
    if ignored:
        lines.append(f"pepi-eval:   ignore top-level key(s): {', '.join(ignored)}")
    if summaries:
        lines.append("pepi-eval: warning: Literal env/header/oauth values are streamed into the container tmpfs, where "
                     "the agent and its MCP tools can read them. OAuth sign-in, keychain and mcp-auth.json flows "
                     "are not supported.")
    return lines


def preview_host_mcp(run: Path) -> int:
    """Print what a host mcp.json copy would contain (names only); stdout: `<count> <sha256>`."""
    source = Path.home() / HOST_MCP
    try:
        content, summaries, dropped, ignored, shared = host_mcp(run, source)
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
        # Only our own messages: parser/OS errors can quote paths or content.
        message = str(error) if type(error) is ValueError and str(error).startswith(("Host mcp.json", "Native MCP")) \
            else "check the frozen MCP policy, source and permissions"
        print(f"pepi-eval: error: Cannot use host mcp.json: {message}", file=sys.stderr)
        return 2
    for line in describe_host_mcp(source, summaries, dropped, ignored, shared):
        print(line, file=sys.stderr)
    print(len(summaries), host_mcp_sha256(content))
    return 0


def run_with_host_auth(run: Path, command: list[str], auth: bool = True, mcp: bool = False,
                       approved_mcp_sha256: str | None = None, skills: bool = False,
                       approved_skills_sha256: str | None = None) -> int:
    """Stage the consented host files in one private directory for the runner child.

    With `approved_mcp_sha256` (the user approved a previewed list), the filtered mcp.json must
    still hash to it; `approved_skills_sha256` pins the skill list the same way. Skills go to a
    second private directory (`--skills-dir`). Never write back to the sources; own cleanup even
    when interrupted during setup.
    """
    child = None
    interrupted = False

    def interrupt(signum, _frame):
        nonlocal interrupted
        interrupted = True
        if child is None:
            raise InterruptedError()
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    signals = (signal.SIGINT, signal.SIGTERM)
    previous = {s: signal.signal(s, interrupt) for s in signals}
    stage = "auth"
    child_env = {name: value for name, value in os.environ.items()
                 if name not in (HOST_MCP_SHA256_ENV, HOST_SKILLS_SHA256_ENV)}
    try:
        files = {}
        if auth:
            files["auth.json"] = required_host_auth(run, Path.home() / HOST_AUTH)
        if mcp:
            stage = "mcp"
            content, *_ = host_mcp(run, Path.home() / HOST_MCP)
            if approved_mcp_sha256 is not None and not hmac.compare_digest(
                    host_mcp_sha256(content).encode(), approved_mcp_sha256.encode("utf-8", "surrogateescape")):
                print("pepi-eval: error: host mcp.json changed since approval; re-run", file=sys.stderr)
                return 2
            if content is None:  # the file changed since the preview: run without it
                print("pepi-eval: warning: No server in the host mcp.json can run in this eval; "
                      "continuing without host MCP servers", file=sys.stderr)
            else:
                files["mcp.json"] = content
        approved_skills = []
        if skills:
            stage = "skills"
            approved_skills, _ = collect_host_skills(Path.home(), credential_values())
            if approved_skills_sha256 is not None and not hmac.compare_digest(
                    skills_sha256(approved_skills).encode(),
                    approved_skills_sha256.encode("utf-8", "surrogateescape")):
                print("pepi-eval: error: host skills changed since approval; re-run", file=sys.stderr)
                return 2
            if not approved_skills:  # the directories changed since the preview: run without them
                print("pepi-eval: warning: No host skill can be used in this eval; continuing without host skills",
                      file=sys.stderr)
            # The runner refuses a skill holding any value it redacts; check the ones known here
            # (gateway URL/key, the auth.json/mcp.json staged above) before anything is staged.
            # Forwarded .env values and --secrets-dir files are still checked by the runner, before
            # any container starts. LITELLM_BASE_URL counts even when no frozen provider needs it.
            known = Secrets(files).values + [value.encode("utf-8", "surrogateescape") for value in
                                             [os.environ.get("LITELLM_BASE_URL", "")] if value]
            holder = skill_holding(approved_skills, known)
            if holder is not None:
                print(f"pepi-eval: error: host skill {holder} contains a credential value of this run (gateway "
                      "URL/key, auth.json or mcp.json); remove it from the skill or decline host skills",
                      file=sys.stderr)
                return 2
        stage = "auth" if auth else "mcp" if mcp else "skills"
        # Ignore TMPDIR: credentials must not end up under a checkout/build/attempt tree.
        # Block setup/launch windows: no signal may strand a just-created directory/child.
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
        try:
            with contextlib.ExitStack() as directories:
                arguments = []
                if auth or mcp:
                    directory = directories.enter_context(
                        tempfile.TemporaryDirectory(prefix="pepi-eval-auth-", dir="/tmp"))
                    os.chmod(directory, 0o700)
                    for name, content in files.items():
                        path = Path(directory) / name
                        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        with os.fdopen(fd, "wb") as stream:
                            os.fchmod(stream.fileno(), 0o600)
                            stream.write(content)
                    arguments += ["--secrets-dir", directory]
                if approved_skills:
                    skills_directory = directories.enter_context(
                        tempfile.TemporaryDirectory(prefix="pepi-eval-skills-", dir="/tmp"))
                    os.chmod(skills_directory, 0o700)
                    stage = "skills"
                    stage_skills(approved_skills, Path(skills_directory))
                    arguments += ["--skills-dir", skills_directory]
                # This adapter is single-threaded; restore the child's signal mask before exec.
                child = subprocess.Popen([*command, *arguments], env=child_env,
                                         preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, mask))
                signal.pthread_sigmask(signal.SIG_SETMASK, mask)
                status = child.wait()
                return 130 if interrupted or status < 0 else status
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
    except InterruptedError:
        return 130
    except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError):
        # Validation exceptions can originate in credential parsers: do not echo their values.
        if stage == "mcp":
            print("pepi-eval: error: Cannot use host mcp.json (check the frozen MCP policy, source and permissions)",
                  file=sys.stderr)
        elif stage == "skills":
            print("pepi-eval: error: Cannot use host skills (check the skills directories and permissions)",
                  file=sys.stderr)
        else:
            print("pepi-eval: error: Cannot use host auth (check frozen plan, provider credentials, source and permissions)",
                  file=sys.stderr)
        return 2
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main() -> int:
    # Internal adapter; the shell calls this only after separate explicit consent:
    #   RUN --mcp-preview                 names-only summary on stderr, `<count> <sha256>` on stdout
    #   RUN [--auth] [--mcp] [--skills] -- COMMAND
    #                                     stage the consented files (no flag: --auth); with --mcp,
    #                                     PEPI_EVAL_HOST_MCP_SHA256 (env, never argv) pins the approval,
    #                                     with --skills, PEPI_EVAL_HOST_SKILLS_SHA256 does
    args = sys.argv[1:]
    if len(args) == 2 and args[1] == "--mcp-preview":
        return preview_host_mcp(Path(args[0]))
    if len(args) < 3 or "--" not in args[1:]:
        print("pepi-eval: error: Invalid host-auth adapter arguments", file=sys.stderr)
        return 2
    split = args.index("--", 1)
    flags, command = args[1:split], args[split + 1:]
    if not command or any(flag not in ("--auth", "--mcp", "--skills") for flag in flags) \
            or len(set(flags)) != len(flags):
        print("pepi-eval: error: Invalid host-auth adapter arguments", file=sys.stderr)
        return 2
    auth = "--auth" in flags or not flags
    mcp = "--mcp" in flags
    skills = "--skills" in flags
    return run_with_host_auth(Path(args[0]), command, auth=auth, mcp=mcp,
                              approved_mcp_sha256=os.environ.get(HOST_MCP_SHA256_ENV) if mcp else None,
                              skills=skills,
                              approved_skills_sha256=os.environ.get(HOST_SKILLS_SHA256_ENV) if skills else None)


if __name__ == "__main__":
    sys.exit(main())
