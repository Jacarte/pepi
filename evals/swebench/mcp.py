"""Native MCP policy and build-time server provisioning (never host configuration)."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .common import read_json, sha256_file, version, write_json

MCP_BIN = "/opt/pepi-mcp/node_modules/.bin/"
PACKAGE_NAME = r"(?:@[a-z0-9._-]+/)?[a-z0-9][a-z0-9._-]*"
COMMAND_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"


def native_mcp_available(pi_version: str) -> bool:
    # 1.1.0 is the verified runtime baseline, not the first Pi release with MCP.
    return ("-" not in pi_version
            and tuple(map(int, version(pi_version).split("."))) >= (1, 1, 0))


def freeze_mcp(source: Callable[[str], Path], build: Path, pi_version: str,
               drop: bool) -> tuple[dict, dict]:
    if not drop and not native_mcp_available(pi_version):
        raise ValueError("Native MCP evaluation requires Pi >= 1.1.0 (stable); use --drop-mcp for older Pi")
    policy = {"enabled": not drop, "commands": []}
    files = {}
    destination = build / "mcp"
    destination.mkdir(parents=True)
    manifest_path = source("mcp-packages.json")
    if not drop and manifest_path.is_file():
        manifest = read_json(manifest_path)
        if not isinstance(manifest, dict) or set(manifest) != {"dependencies", "commands"}:
            raise ValueError("mcp-packages.json must contain dependencies and commands only")
        dependencies, commands = manifest["dependencies"], manifest["commands"]
        if not isinstance(dependencies, dict) or not dependencies:
            raise ValueError("MCP dependencies must be a nonempty object of exact npm versions")
        for name, pinned in dependencies.items():
            if not re.fullmatch(PACKAGE_NAME, name) or not isinstance(pinned, str):
                raise ValueError("Invalid MCP npm dependency")
            version(pinned)
        if (not isinstance(commands, list) or not commands
                or any(not isinstance(c, str) or not re.fullmatch(COMMAND_NAME, c) for c in commands)
                or len(commands) != len(set(commands))):
            raise ValueError("MCP commands must be unique npm binary names")
        # Keep the manifest and its lock together; do not mix injected and repo locks.
        lock_path = manifest_path.with_name("mcp-package-lock.json")
        lock = read_json(lock_path)
        packages = lock.get("packages") if isinstance(lock, dict) else None
        if (not isinstance(lock, dict) or lock.get("lockfileVersion") != 3
                or not isinstance(packages, dict) or not isinstance(packages.get(""), dict)
                or packages[""].get("dependencies") != dependencies
                or any(not isinstance(p, dict) for p in packages.values())):
            raise ValueError("MCP lockfile v3 must match mcp-packages.json dependencies")
        bins = set()
        for name in dependencies:
            package = packages.get(f"node_modules/{name}", {})
            if package.get("version") != dependencies[name]:
                raise ValueError("MCP lockfile must pin each direct dependency")
            entry = package.get("bin", {})
            if isinstance(entry, dict):
                bins.update(entry)
            elif isinstance(entry, str):
                bins.add(name.rsplit("/", 1)[-1])
        if not set(commands) <= bins:
            raise ValueError("MCP commands must be binaries from direct locked dependencies")
        for path, package in packages.items():
            if not path:
                continue
            resolved_value, integrity = package.get("resolved", ""), package.get("integrity", "")
            if not isinstance(resolved_value, str) or not isinstance(integrity, str):
                raise ValueError("MCP lock entries require resolved URLs and integrity strings")
            resolved = urlsplit(resolved_value)
            if (package.get("link") or resolved.scheme != "https"
                    or resolved.netloc != "registry.npmjs.org" or resolved.query or resolved.fragment
                    or not re.fullmatch(r"sha512-[A-Za-z0-9+/]{86}==", integrity)):
                raise ValueError("MCP lock entries require npm registry HTTPS URLs and SHA-512 integrity")
        write_json(destination / "package.json", {"private": True, "dependencies": dependencies})
        shutil.copyfile(lock_path, destination / "package-lock.json")
        policy["commands"] = sorted(commands)
        files = {"mcp-packages.json": sha256_file(manifest_path),
                 "mcp-package-lock.json": sha256_file(lock_path)}
    write_json(destination / "policy.json", policy)
    return policy, files


def validate_mcp_secret(content: bytes, policy: dict, pi_version: str) -> None:
    """Reject unsupported transports/launchers before any task container starts."""
    if not policy.get("enabled"):
        raise ValueError("mcp.json is not allowed for a profile prepared with --drop-mcp (or a legacy profile)")
    if not native_mcp_available(pi_version):
        raise ValueError("mcp.json requires Pi >= 1.1.0")
    try:
        config = json.loads(content)
    except (ValueError, UnicodeError):
        raise ValueError("mcp.json must be valid JSON") from None
    if (not isinstance(config, dict) or set(config) - {"mcpServers", "autoEnableCodemode"}
            or not isinstance(config.get("mcpServers"), dict)):
        raise ValueError("Use native Pi mcp.json with a mcpServers object, not adapter settings/imports")
    allowed = {MCP_BIN + command for command in policy["commands"]}
    names = set()
    for name, server in config["mcpServers"].items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or name.replace("-", "_") in names:
            raise ValueError("MCP server names must be native Pi identifiers without normalized collisions")
        names.add(name.replace("-", "_"))
        if not isinstance(server, dict):
            raise ValueError("MCP server entries must be objects")
        if "command" in server:
            if (server.get("type", "stdio") != "stdio" or "url" in server
                    or not isinstance(server["command"], str) or server["command"] not in allowed):
                raise ValueError("MCP stdio command must be a provisioned " + MCP_BIN + "<binary>; "
                                 "declare exact packages in mcp-packages.json (no npx/uvx/custom commands)")
        elif "url" in server:
            url = urlsplit(server["url"]) if isinstance(server["url"], str) else None
            if (server.get("type", "http") not in ("http", "streamable-http")
                    or url is None or url.scheme not in ("https", "http") or not url.hostname):
                raise ValueError("MCP remote servers require native streamable HTTP URLs")
        else:
            raise ValueError("MCP server requires a provisioned command or HTTP URL")
        if "args" in server and (not isinstance(server["args"], list)
                                 or any(not isinstance(arg, str) for arg in server["args"])):
            raise ValueError("MCP args must be an array of strings")
        # Pi can execute !command in secret fields. This bounded runtime accepts literal
        # credentials/env references only; otherwise they bypass provisioned launchers.
        for field in ("env", "headers", "oauth"):
            values = server.get(field, {})
            if (not isinstance(values, dict)
                    or any(isinstance(v, str) and v.lstrip().startswith("!") for v in values.values())
                    or field != "oauth" and any(not isinstance(v, str) for v in values.values())):
                raise ValueError("MCP env/headers/oauth must be objects without !command values")
