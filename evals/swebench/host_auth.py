"""Explicit-consent CLI adapter: filtered host auth lives only for one runner child."""
from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from .common import read_manifest
from .runner import Secrets, frozen_provider_plan


def required_host_auth(run: Path, source: Path) -> bytes:
    """Validate the frozen contract before opening a private, no-follow source FD."""
    plan = frozen_provider_plan(run)
    version = read_manifest(run)["versions"]["pi"]
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or not info.st_mode & stat.S_IRUSR):
            raise ValueError("Host auth must be a readable private regular file owned by this user")
        content = stream.read(1048577)
    if len(content) > 1048576:
        raise ValueError("Host auth is too large")
    auth = json.loads(content)
    if not isinstance(auth, dict):
        raise ValueError("Host auth must be an object")
    # Explicit host opt-in requires every planned provider in the source, not an env fallback.
    if any(name not in auth for name in plan["required_providers"]):
        raise ValueError("Host auth is missing a frozen provider")
    content = json.dumps({name: auth[name] for name in plan["required_providers"]}).encode()
    Secrets({"auth.json": content}).validate(plan, version)
    return content


def run_with_host_auth(run: Path, command: list[str]) -> int:
    """Never write back to the source; own cleanup even when interrupted during setup."""
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
    try:
        content = required_host_auth(run, Path.home() / ".pi/agent/auth.json")
        # Ignore TMPDIR: credentials must not end up under a checkout/build/attempt tree.
        # Block setup/launch windows: no signal may strand a just-created directory/child.
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
        try:
            with tempfile.TemporaryDirectory(prefix="pepi-eval-auth-", dir="/tmp") as directory:
                os.chmod(directory, 0o700)
                path = Path(directory) / "auth.json"
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(content)
                # This adapter is single-threaded; restore the child's signal mask before exec.
                child = subprocess.Popen([*command, "--secrets-dir", directory],
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
        print("pepi-eval: error: Cannot use host auth (check frozen plan, provider credentials, source and permissions)",
              file=sys.stderr)
        return 2
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main() -> int:
    # Internal adapter; the shell calls this only after separate explicit host-auth consent.
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        print("pepi-eval: error: Invalid host-auth adapter arguments", file=sys.stderr)
        return 2
    return run_with_host_auth(Path(sys.argv[1]), sys.argv[3:])


if __name__ == "__main__":
    sys.exit(main())
