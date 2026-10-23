"""One chat interface over the two endpoint kinds an admin can pick in Settings:
Claude-compatible (official `anthropic` SDK) and OpenAI-compatible (official `openai` SDK), each with its
own base URL, key and model. Conversation keeps each provider's native message list, so assistant turns
(including Claude's thinking blocks) go back to the API unchanged inside a tool loop."""
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field

import httpx

from . import db

# Defaults come from the environment (install.sh points them at the local model when LOCAL_LLM is used);
# anything an admin saves in Settings overrides them.
env = lambda key, default: os.environ.get(key) or default  # blank in .env counts as unset
DEFAULTS = {"llm_kind": env("LLM_KIND", "anthropic"), "llm_base_url": env("LLM_BASE_URL", ""),
            "llm_model": env("LLM_MODEL", "claude-opus-5-5"),
            "llm_effort": env("LLM_EFFORT", "high")}  # Claude only; "(don't send)" is chosen in Settings
TIMEOUT = float(env("LLM_TIMEOUT", "600"))  # seconds; a local CPU model needs far longer than an API
OFFICIAL_ANTHROPIC = {"", "https://api.anthropic.com", "https://api.anthropic.com/"}
# Models that take server-side refusal fallback ("default" routes by refusal category; Claude API only).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}


class LLMError(RuntimeError):
    pass


def config():
    cfg = {k: db.setting(k, v) for k, v in DEFAULTS.items()}
    cfg["llm_api_key"] = db.setting("llm_api_key", "") or os.environ.get("LLM_API_KEY", "")
    return cfg


def usable(cfg):
    """A key, or an OpenAI-compatible server of our own (Ollama, llama.cpp, vLLM), which needs none."""
    return bool(cfg["llm_api_key"] or (cfg["llm_kind"] == "openai" and cfg["llm_base_url"]))


def configured():
    return usable(config())


OLLAMA_URL = env("OLLAMA_URL", "http://llm:11434").rstrip("/")  # the compose "llm" service (install.sh LOCAL_LLM)


def local_models():
    """Models already downloaded into the local Ollama; [] when there is none."""
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        r.raise_for_status()
        return [{"name": m["name"], "size": m.get("size", 0), "params": m.get("details", {}).get("parameter_size"),
                 "quant": m.get("details", {}).get("quantization_level")} for m in r.json().get("models", [])]
    except (httpx.HTTPError, ValueError, KeyError):
        return []


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Reply:
    text: str
    tool_calls: list = field(default_factory=list)


def anthropic_tools(tools):
    return [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]


def openai_tools(tools):
    return [{"type": "function", "function": t} for t in tools]


def parse_args(raw):
    """OpenAI returns tool arguments as a JSON string; a model can emit invalid JSON."""
    try:
        args = json.loads(raw or "{}")
        return args if isinstance(args, dict) else {}
    except json.JSONDecodeError:
        return {}


# Ask goes first. A local model answers one or two requests at a time, so documentation and reviews (background)
# send one request at a time and start none while someone is waiting for an answer in Ask.
_cond, _chatting, _background = threading.Condition(), 0, threading.Lock()


@contextmanager
def interactive():
    global _chatting
    with _cond:
        _chatting += 1
    try:
        yield
    finally:
        with _cond:
            _chatting -= 1
            _cond.notify_all()


@contextmanager
def background():
    with _background:
        with _cond:  # ponytail: waits at most 15 min, so a stuck chat can't stop docs and reviews for good
            _cond.wait_for(lambda: _chatting == 0, timeout=900)
        yield


class Conversation:
    def __init__(self, system, cfg=None, timeout=TIMEOUT, retries=3, cache=False):
        """cache: Claude caches the prompt prefix (tools, system, conversation so far), so each round of a tool loop
        reads it back instead of processing it again. Off for one-shot requests, where the cache write only costs."""
        self.cfg, self.cache = cfg or config(), cache
        if not usable(self.cfg):
            raise LLMError("No LLM is configured. An admin can set one in Settings.")
        self.system, self.messages = system, []
        self.kind, self.model = self.cfg["llm_kind"], self.cfg["llm_model"]
        base_url = self.cfg.get("llm_base_url") or None
        if self.kind == "anthropic":
            import anthropic
            self.client = anthropic.Anthropic(api_key=self.cfg["llm_api_key"], base_url=base_url, max_retries=retries,
                                              timeout=timeout)
        else:
            import openai  # the SDK insists on a key; local servers ignore it
            self.client = openai.OpenAI(api_key=self.cfg["llm_api_key"] or "none", base_url=base_url,
                                        max_retries=retries, timeout=timeout)

    def add_user(self, text):
        self.messages.append({"role": "user", "content": text})

    def add_assistant_text(self, text):
        self.messages.append({"role": "assistant", "content": text})

    def step(self, tools=()):
        try:
            return self._anthropic(tools) if self.kind == "anthropic" else self._openai(tools)
        except LLMError:
            raise
        except Exception as e:  # SDK errors (auth, rate limit, bad model) surface to the UI as text
            raise LLMError(f"{type(e).__name__}: {e}") from e

    def add_tool_results(self, results):
        """results: [(tool_call_id, content)], all from one assistant turn."""
        if self.kind == "anthropic":  # every result for the turn goes back in one user message
            self.messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": cid, "content": content} for cid, content in results]})
        else:
            self.messages += [{"role": "tool", "tool_call_id": cid, "content": content} for cid, content in results]

    def _anthropic(self, tools):
        kw = {"model": self.model, "max_tokens": 16000, "system": self.system, "messages": self.messages}
        if tools:
            kw["tools"] = anthropic_tools(tools)
        if self.cfg.get("llm_effort"):
            kw["output_config"] = {"effort": self.cfg["llm_effort"]}
        if self.cache:
            kw["cache_control"] = {"type": "ephemeral"}  # the last block: the whole prefix so far
        if (self.cfg.get("llm_base_url") or "") in OFFICIAL_ANTHROPIC and self.model in FALLBACK_MODELS:
            resp = self.client.beta.messages.create(**kw, betas=["server-side-fallback-2026-07-01"],
                                                    extra_body={"fallbacks": "default"})
        else:
            resp = self.client.messages.create(**kw)
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            raise LLMError(f"The model declined this request ({getattr(details, 'category', None) or 'no category'}).")
        self.messages.append({"role": "assistant", "content": resp.content})  # unchanged, thinking blocks included
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, b.input if isinstance(b.input, dict) else {})
                 for b in resp.content if b.type == "tool_use"]
        return Reply(text, calls)

    def _openai(self, tools):
        kw = {"model": self.model, "messages": [{"role": "system", "content": self.system}, *self.messages]}
        if tools:
            kw["tools"] = openai_tools(tools)
        msg = self.client.chat.completions.create(**kw).choices[0].message
        calls = [ToolCall(c.id, c.function.name, parse_args(c.function.arguments)) for c in msg.tool_calls or []]
        turn = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            turn["tool_calls"] = [c.model_dump() for c in msg.tool_calls]
        self.messages.append(turn)
        return Reply(msg.content or "", calls)


NOOP = {"name": "noop", "description": "Does nothing. Do not call it.", "parameters": {"type": "object", "properties": {}}}


def ping(cfg):
    """Settings 'Test' button: one tiny request with the given (unsaved) config. It offers a tool, because Ask needs
    tool calling and some servers or models reject tools outright; that should show up here, not in Ask."""
    conv = Conversation("Reply with the single word OK.", cfg, timeout=120, retries=0)
    conv.add_user("ping")
    try:
        reply = conv.step([NOOP])
    except LLMError:  # no tools: fine for reviews and docs, not for Ask; a broken endpoint still raises below
        return {"reply": conv.step().text.strip()[:200], "tools": False}
    return {"reply": (reply.text.strip() or "(answered with a tool call)")[:200], "tools": True}


def models(cfg):
    """Model ids the endpoint offers (Settings 'Load models'); both SDKs page through them."""
    conv = Conversation("", cfg, timeout=30, retries=0)
    try:
        return sorted(m.id for m in conv.client.models.list())[:1000]
    except Exception as e:
        raise LLMError(f"{type(e).__name__}: {e}") from e
