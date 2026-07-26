#!/usr/bin/env python3
"""llm_provider.py — provider-agnostic structured-JSON LLM call for the edge pipeline.

Local Ollama by DEFAULT (self-contained, no key). A shipped user MAY route the discovery +
typing (classify_edges) generate calls to a cloud provider for higher precision, via graph.yaml's
`llm:` block. WE never set a cloud provider — this keeps our brain 100% local.

The API KEY is NEVER stored in config or on disk — graph.yaml holds only the NAME of the env var
that carries it (`key_env`); the value is read from the environment at call time (systemd
EnvironmentFile / shell). With no cloud provider configured, every call runs on the existing local
Ollama endpoint UNCHANGED (byte-for-byte the prior request).

One entry point: llm_json(prompt, schema, num_predict, timeout) -> dict | None. `schema` is a JSON
Schema for the desired object; each provider is asked to return an object conforming to it:
  ollama    -> /api/generate with `format=<schema>` (Ollama structured outputs) — the current path
  openai    -> /chat/completions with response_format json_schema (OpenAI-compatible; also Azure /
               OpenRouter / vLLM etc. via base_url)
  anthropic -> /v1/messages with ONE forced tool whose input_schema IS the schema; the tool call's
               `input` is the object (docs: structured output via forced tool_use)
Returns the parsed dict, or None on any failure (callers already treat None as a transient miss and
skip/retry). Cloud requests deliberately omit temperature/thinking — those 400 on Opus 4.7/4.8.
"""
import json
import os
import sys
import urllib.request

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_cfg():
    try:
        with open(os.path.join(_HERE, "graph.yaml")) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


_CFG = _load_cfg()
_LLM = (_CFG.get("llm") or {})
_OLL = (_CFG.get("ollama") or {})

PROVIDER = (_LLM.get("provider") or "ollama").strip().lower()
# Ollama defaults come from the existing `ollama:` block (backward compatible; this is our path).
OLLAMA_URL = _OLL.get("endpoint") or "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = _OLL.get("model") or "qwen3:30b-a3b-instruct-2507-q4_K_M"
OLLAMA_TIMEOUT = int(_OLL.get("timeout") or 60)
# Cloud settings — used ONLY when provider != ollama. Key value never lives here.
CLOUD_BASE = (_LLM.get("base_url") or "").rstrip("/")
CLOUD_MODEL = _LLM.get("model") or ""
CLOUD_KEY_ENV = _LLM.get("key_env") or ""
CLOUD_TIMEOUT = int(_LLM.get("timeout") or OLLAMA_TIMEOUT)
ANTHROPIC_VERSION = _LLM.get("anthropic_version") or "2023-06-01"


def _post(url, body, headers, timeout):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _cloud_key():
    """Read the cloud API key from the env var NAMED in graph.yaml (never the value in config)."""
    if not CLOUD_KEY_ENV:
        raise RuntimeError("llm.key_env is not set in graph.yaml — name the env var holding the API key")
    key = os.environ.get(CLOUD_KEY_ENV)
    if not key:
        raise RuntimeError("env var %s (llm.key_env) is empty — set the cloud API key there" % CLOUD_KEY_ENV)
    return key


def _need_model():
    if not CLOUD_MODEL:
        raise RuntimeError("llm.model is not set in graph.yaml for provider=%s" % PROVIDER)
    return CLOUD_MODEL


def _ollama(prompt, schema, num_predict, timeout):
    body = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
            "format": schema, "options": {"temperature": 0, "num_predict": num_predict}}
    resp = _post(OLLAMA_URL, body, {"Content-Type": "application/json"}, timeout or OLLAMA_TIMEOUT)
    return json.loads(resp.get("response", ""))


def _openai(prompt, schema, num_predict, timeout):
    url = (CLOUD_BASE or "https://api.openai.com/v1") + "/chat/completions"
    body = {"model": _need_model(), "max_tokens": num_predict,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "out", "schema": schema}}}
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + _cloud_key()}
    resp = _post(url, body, headers, timeout or CLOUD_TIMEOUT)
    return json.loads(resp["choices"][0]["message"]["content"])


def _anthropic(prompt, schema, num_predict, timeout):
    # Structured output via a single forced tool whose input_schema is the caller's schema.
    # No temperature / thinking — both 400 on Opus 4.7/4.8.
    url = (CLOUD_BASE or "https://api.anthropic.com") + "/v1/messages"
    tool = {"name": "emit", "description": "Return the result as structured JSON.",
            "input_schema": schema}
    body = {"model": _need_model(), "max_tokens": num_predict, "tools": [tool],
            "tool_choice": {"type": "tool", "name": "emit"},
            "messages": [{"role": "user", "content": prompt}]}
    headers = {"Content-Type": "application/json", "x-api-key": _cloud_key(),
               "anthropic-version": ANTHROPIC_VERSION}
    resp = _post(url, body, headers, timeout or CLOUD_TIMEOUT)
    for block in (resp.get("content") or []):
        if block.get("type") == "tool_use":
            return block.get("input")
    return None


def llm_json(prompt, schema, num_predict=2048, timeout=None):
    """Structured-JSON completion via the configured provider. Returns dict or None on any failure."""
    try:
        if PROVIDER == "openai":
            return _openai(prompt, schema, num_predict, timeout)
        if PROVIDER == "anthropic":
            return _anthropic(prompt, schema, num_predict, timeout)
        return _ollama(prompt, schema, num_predict, timeout)
    except Exception as e:
        sys.stderr.write("  ! llm_json (provider=%s) failed: %s\n" % (PROVIDER, e))
        return None
