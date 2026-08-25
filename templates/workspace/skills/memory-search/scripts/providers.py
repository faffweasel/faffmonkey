"""Embedding provider abstraction for memory-search skill.

Reads config from skills-data/memory-search/config.json. Supports
OpenAI-compatible and Ollama /api/embed formats. Returns None if no
provider is available (FTS5-only mode).
"""
from __future__ import annotations

import json
import os
import struct
import urllib.error
import urllib.request
from pathlib import Path

USER_AGENT = "Mozilla/5.0 (faffmonkey memory-search)"


def load_config(skill_data: Path) -> dict | None:
    config_path = skill_data / "config.json"
    if not config_path.exists():
        return None
    try:
        cfg = json.loads(config_path.read_text())
        return cfg.get("embedding")
    except (json.JSONDecodeError, OSError):
        return None


def _embed_openai_compat(text: str, endpoint: str, api_key: str, model: str) -> list[float] | None:
    body = json.dumps({"input": text, "model": model}).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(endpoint, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return data["data"][0]["embedding"]
    except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError, OSError):
        return None


def _embed_ollama(text: str, endpoint: str, model: str) -> list[float] | None:
    body = json.dumps({"input": text, "model": model}).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    req = urllib.request.Request(endpoint, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return data["embeddings"][0]
    except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError, OSError):
        return None


def embed(text: str, embedding_config: dict) -> list[float] | None:
    providers = embedding_config.get("providers", {})
    preferred = embedding_config.get("provider", "auto")

    # "none" (the default) means no embedding calls at all, and a preferred
    # provider that is not configured must not silently widen to "auto":
    # memory content is personal data and goes only where the operator
    # explicitly pointed it.
    if preferred in ("none", "off", "disabled"):
        return None
    if preferred == "auto":
        items = list(providers.items())
    elif preferred in providers:
        items = [(preferred, providers[preferred])]
    else:
        return None

    for name, pcfg in items:
        endpoint = pcfg.get("endpoint", "")
        model = pcfg.get("model", "")
        fmt = pcfg.get("format", "openai")
        env_var = pcfg.get("apiKeyEnvVar", "")

        if not endpoint or not model:
            continue

        if fmt == "openai":
            api_key = ""
            if env_var:
                api_key = os.environ.get(env_var, "").strip()
                if not api_key:
                    continue
            result = _embed_openai_compat(text, endpoint, api_key, model)
        elif fmt == "ollama":
            result = _embed_ollama(text, endpoint, model)
        else:
            continue

        if result is not None:
            return result
    return None


def vec_to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def blob_to_vec(blob: bytes) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
