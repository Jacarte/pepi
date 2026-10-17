"""Eval-only configuration policy; no host credentials or extension discovery."""
from __future__ import annotations

import copy
import math
import re
from pathlib import Path
from urllib.parse import urlsplit

from .common import identifier, read_json, reject_secret_files, version

# Pi 1.1.0 bundled pi-ai provider catalog (dist/providers/data); extensions are not installed.
BUILTIN_PROVIDERS = frozenset("amazon-bedrock ant-ling anthropic azure baseten cerebras "
    "cloudflare-ai-gateway cloudflare-workers-ai deepseek fireworks github-copilot google-vertex "
    "google groq huggingface kimi-coding meta minimax-cn minimax mistral moonshotai-cn moonshotai "
    "nvidia openai-codex openai opencode-go opencode openrouter qwen-token-plan-cn "
    "qwen-token-plan-individual qwen-token-plan radius together typesafe vercel-ai-gateway xai "
    "xiaomi-token-plan-ams xiaomi-token-plan-cn xiaomi-token-plan-sgp xiaomi zai-coding-cn zai".split())
PROVIDERS = BUILTIN_PROVIDERS | {"litellm"}
ROLE_FIELDS = {"model", "defaultProvider", "thinking", "disabled", "fast", "tools"}
THINKING = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
SUBAGENT_FIELDS = {"defaultModel", "defaultProvider", "defaultThinking", "disableThinking",
                   "maxThinking", "projectRootResolution", "agentOverrides", "agentOverridesByProvider"}


def safe_path(base: Path, name: str) -> Path:
    """Reject links before reading/copying, including ancestor and directory contents."""
    path = base / name
    for ancestor in (base, *base.parents):
        # macOS system temp aliases are not user-selected configuration links.
        if ancestor.is_symlink() and ancestor not in (Path("/var"), Path("/tmp")):
            raise ValueError("Symlinks are not allowed in config sources")
    current = base
    for part in Path(name).parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Symlinks are not allowed in config sources")
    if path.is_dir():
        for child in path.rglob("*"):
            if child.is_symlink() or not (child.is_file() or child.is_dir()):
                raise ValueError("Symlinks and special files are not allowed in config sources")
    elif path.exists() and not path.is_file():
        raise ValueError("Config sources must be regular files or directories")
    return path


def config_source(root: Path, config_dir: Path | None, profile_name: str | None):
    if config_dir is not None:
        safe_path(config_dir, "")
        if not config_dir.is_dir():
            raise ValueError("Config directory not found")
        reject_secret_files(config_dir)
    named = None
    if profile_name is not None:
        identifier(profile_name)
        named = safe_path(root, f"profiles/{profile_name}")
        if not named.is_dir() or not safe_path(named, "settings.json").is_file():
            raise ValueError("Named profile must contain settings.json")
    def source(name: str) -> Path:
        for directory in (config_dir, named):
            if directory is not None:
                candidate = safe_path(directory, name)
                if candidate.exists():
                    return candidate
        # Named profiles without models must not inherit a previously active profile's models.
        return safe_path(named if named is not None and name == "models.json" else root, name)
    return source


def provider(value) -> str:
    if not isinstance(value, str) or value not in PROVIDERS:
        raise ValueError("Unsupported provider dependency; only Pi 1.1.0 builtins and pinned LiteLLM are provisioned")
    return value


def model_choice(value, preferred: str, parent: dict | None = None) -> dict:
    if value == "inherit" and parent is not None:
        return dict(parent)
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]*", value):
        raise ValueError("Models must be explicit identifiers (no commands, interpolation or patterns)")
    if any(value.startswith(p + separator) for p in PROVIDERS for separator in (":", ".")):
        raise ValueError("Ambiguous model routing; use explicit provider/model identifiers")
    head, slash, tail = value.partition("/")
    if slash and head in PROVIDERS:
        if not tail or tail.endswith("/") or "//" in tail:
            raise ValueError("Models must have explicit nonempty identifiers")
        return {"provider": provider(head), "model": tail}
    if slash:
        raise ValueError("Ambiguous model routing; use explicit provider/model identifiers")
    return {"provider": provider(preferred), "model": value}


def isolated_settings(settings: dict, versions: dict | None) -> tuple[dict, dict]:
    if not isinstance(settings, dict):
        raise ValueError("settings.json must be an object")
    parent = model_choice(settings.get("defaultModel"), provider(settings.get("defaultProvider")))
    frozen = {"defaultProvider": parent["provider"], "defaultModel": parent["model"]}
    if "defaultThinkingLevel" in settings:
        thinking = settings["defaultThinkingLevel"]
        if not isinstance(thinking, str) or thinking not in THINKING:
            raise ValueError("Invalid defaultThinkingLevel")
        frozen["defaultThinkingLevel"] = thinking
    original = settings.get("subagents", {})
    if not isinstance(original, dict):
        raise ValueError("subagents must be an object")
    sub = {key: copy.deepcopy(value) for key, value in original.items() if key in SUBAGENT_FIELDS}
    for key in ("defaultThinking", "maxThinking"):
        if key in sub and (not isinstance(sub[key], str) or sub[key] not in THINKING):
            raise ValueError("Invalid subagent thinking level")
    if "disableThinking" in sub and type(sub["disableThinking"]) is not bool:
        raise ValueError("disableThinking must be boolean")
    if "projectRootResolution" in sub and sub["projectRootResolution"] not in ("git-root", "cwd"):
        raise ValueError("Unsupported projectRootResolution")
    choices = [parent]
    preferred = provider(sub.get("defaultProvider", parent["provider"]))
    raw_default_model = sub.get("defaultModel", "inherit")
    default = model_choice(raw_default_model, preferred, parent)
    sub["defaultProvider"] = preferred
    if "defaultModel" in sub and sub["defaultModel"] != "inherit":
        sub["defaultModel"] = default["provider"] + "/" + default["model"]
    choices.append(default)
    def roles(overrides, defaults):
        if not isinstance(overrides, dict):
            raise ValueError("Subagent role overrides must be objects")
        result = {}
        for name, fields in overrides.items():
            identifier(name)
            if not isinstance(fields, dict):
                raise ValueError("Subagent role fields must be objects")
            effective = {**defaults.get(name, {}), **fields}
            for key in ("disabled", "fast"):
                if key in effective and type(effective[key]) is not bool:
                    raise ValueError("Subagent disabled/fast fields must be boolean")
            if "thinking" in effective and effective["thinking"] is not False:
                if not isinstance(effective["thinking"], str) or effective["thinking"] not in THINKING:
                    raise ValueError("Invalid subagent thinking level")
            if "tools" in effective and (not isinstance(effective["tools"], list) or any(
                    not isinstance(tool, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", tool)
                    for tool in effective["tools"])):
                raise ValueError("Subagent tools must be explicit names")
            # External adapters are not provisioned; retain explicit disabled roles.
            if name in ("claude-code", "claude-code-writer") and not effective.get("disabled"):
                raise ValueError("External Claude Code adapters must be disabled for evals")
            result[name] = {k: copy.deepcopy(v) for k, v in fields.items() if k in ROLE_FIELDS}
            if effective.get("disabled"):
                continue
            requested = effective.get("model", raw_default_model)
            choice = model_choice(requested, provider(effective.get("defaultProvider", preferred)), parent)
            result[name]["model"] = ("inherit" if requested == "inherit" else
                                     choice["provider"] + "/" + choice["model"])
            result[name]["defaultProvider"] = choice["provider"]
            choices.append(choice)
        return result
    # Merge scoped preferences with raw role/default models before adding provider qualification.
    raw_overrides = sub.get("agentOverrides", {})
    sub["agentOverrides"] = roles(raw_overrides, {})
    scoped = sub.get("agentOverridesByProvider", {})
    if not isinstance(scoped, dict):
        raise ValueError("Provider-scoped overrides must be an object")
    sub["agentOverridesByProvider"] = {provider(p): roles(r, raw_overrides) for p, r in scoped.items()}
    for name in ("claude-code", "claude-code-writer"):
        sub["agentOverrides"].setdefault(name, {})["disabled"] = True
    frozen["subagents"] = sub
    required = sorted({choice["provider"] for choice in choices})
    if len(required) > 1 and "defaultProvider" not in original:
        raw_roles = list(original.get("agentOverrides", {}).values())
        for overrides in original.get("agentOverridesByProvider", {}).values():
            raw_roles.extend(overrides.values())
        def ambiguous(fields):
            model = fields.get("model")
            return (not fields.get("disabled") and "defaultProvider" not in fields
                    and isinstance(model, str) and model != "inherit" and "/" not in model)
        if ambiguous({"model": original.get("defaultModel")}) or any(ambiguous(r) for r in raw_roles):
            raise ValueError("Ambiguous mixed-provider child routing; qualify models or set subagents.defaultProvider")
    pins = {name: version(versions[name]) for name in ("pi", "pi-subagents")} if versions is not None else {}
    if "litellm" in required:
        if versions is not None and not versions.get("pi-provider-litellm"):
            raise ValueError("LiteLLM is required by a frozen model; supply --litellm-version")
        if versions is not None:
            pins["pi-provider-litellm"] = version(versions["pi-provider-litellm"])
        frozen["litellm"] = {key: {"enabled": False} for key in ("skills", "mcp", "budget")}
    frozen["packages"] = [f"npm:{name}@{pin}" for name, pin in pins.items() if name != "pi"]
    return frozen, {"schema_version": 1, "parent": parent, "models": sorted(
        [dict(provider=p, model=m) for p, m in {(c['provider'], c['model']) for c in choices}],
        key=lambda c: (c["provider"], c["model"])), "required_providers": required, "versions": pins}


MODEL_NUMBERS = {"contextWindow", "maxTokens"}
MODEL_FIELDS = MODEL_NUMBERS | {"id", "name", "reasoning", "input", "cost", "api"}
APIS = {"openai-completions", "openai-responses", "anthropic-messages", "google-generative-ai"}


def safe_models(path: Path) -> dict:
    """Accept narrow metadata/definitions; never headers, apiKey or executable references."""
    data = read_json(path)
    def fail():
        raise ValueError("Unsafe models.json: only literal model metadata/definitions on provisioned providers are allowed")
    if not isinstance(data, dict) or set(data) != {"providers"} or not isinstance(data["providers"], dict):
        fail()
    def metadata(item, definition=False):
        if not isinstance(item, dict) or set(item) - MODEL_FIELDS:
            fail()
        if definition and "id" not in item:
            fail()
        for key, value in item.items():
            if key in MODEL_NUMBERS:
                if type(value) is not int or value <= 0: fail()
            elif key == "reasoning":
                if type(value) is not bool: fail()
            elif key == "input":
                if not isinstance(value, list) or any(v not in ("text", "image") for v in value): fail()
            elif key == "cost":
                if not isinstance(value, dict) or set(value) - {"input", "output", "cacheRead", "cacheWrite"}: fail()
                if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in value.values()): fail()
            elif key == "api":
                if not isinstance(value, str) or value not in APIS: fail()
            elif not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ./_:-]*", value):
                fail()
    for name, config in data["providers"].items():
        provider(name)
        if not isinstance(config, dict) or set(config) - {"models", "modelOverrides", "baseUrl", "api"}: fail()
        if "baseUrl" in config:
            validate_url(config["baseUrl"])
        if "api" in config and (not isinstance(config["api"], str) or config["api"] not in APIS): fail()
        if "models" in config:
            if not isinstance(config["models"], list): fail()
            for item in config["models"]: metadata(item, True)
        overrides = config.get("modelOverrides", {})
        if not isinstance(overrides, dict): fail()
        for model, item in overrides.items():
            model_choice(model, name)
            metadata(item)
    return data


def validate_url(value):
    if not isinstance(value, str):
        raise ValueError("Expected a literal HTTP(S) endpoint")
    url = urlsplit(value)
    if (url.scheme not in ("http", "https") or not url.hostname or url.username or url.password
            or url.query or url.fragment or any(c.isspace() or ord(c) < 32 or c in "!$" for c in value)):
        raise ValueError("Expected a literal HTTP(S) endpoint without credentials, query or interpolation")
    return value
