"""OpenAI-compatible HTTP client shared by every model call.

One httpx2 transport carries the TLS settings, so the chat, extraction, and
embedding endpoints all use the same CA, client certificate, and proxy
environment. Milestone 2 adds the PydanticAI models on top of this client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx2
import openai

from brain.core.config import LLMConfig, Settings
from brain.core.tls import ssl_context


class LLMConfigError(Exception):
    pass


def http_client(settings: Settings, *, transport: httpx2.BaseTransport | None = None) -> httpx2.Client:
    return httpx2.Client(
        verify=ssl_context(settings.tls),
        timeout=httpx2.Timeout(settings.llm.timeout_seconds, connect=15.0),
        transport=transport,
        trust_env=True,
    )


def openai_client(settings: Settings, *, transport: httpx2.BaseTransport | None = None) -> openai.OpenAI:
    cfg = settings.llm
    if not cfg.base_url:
        raise LLMConfigError("llm.base_url is not set")
    key = cfg.api_key()
    if not key:
        raise LLMConfigError(f"the API key environment variable {cfg.api_key_env} is not set")
    return openai.OpenAI(
        base_url=cfg.base_url,
        api_key=key,
        http_client=http_client(settings, transport=transport),
        max_retries=2,
    )


@dataclass(frozen=True)
class EmbedResult:
    vectors: list[list[float]]
    model: str

    @property
    def dim(self) -> int:
        return len(self.vectors[0]) if self.vectors else 0


def embed(client: openai.OpenAI, cfg: LLMConfig, texts: list[str]) -> EmbedResult:
    resp = client.embeddings.create(model=cfg.embed_model, input=texts)
    ordered = sorted(resp.data, key=lambda d: d.index)
    return EmbedResult([list(d.embedding) for d in ordered], resp.model or cfg.embed_model)


def chat_ping(client: openai.OpenAI, model: str) -> str:
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Reply with the single word: ok"}],
        max_tokens=5,
    )
    return (resp.choices[0].message.content or "").strip()


def list_models(client: openai.OpenAI) -> list[str]:
    return [m.id for m in client.models.list().data]


def explain_error(e: Exception, cfg: LLMConfig) -> str:
    """Turn a client exception into a message that says what to change."""
    text = str(e)
    if isinstance(e, openai.AuthenticationError):
        return f"the gateway rejected the API key (401). Check the value of {cfg.api_key_env}."
    if isinstance(e, openai.PermissionDeniedError):
        return "the gateway refused the request (403). The key may not have access to this model."
    if isinstance(e, openai.NotFoundError):
        return f"not found (404): {text}. Check the model name and that llm.base_url ends in the API root (often /v1)."
    if isinstance(e, openai.APIConnectionError):
        cause: Any = e.__cause__
        detail = str(cause or e)
        if "CERTIFICATE_VERIFY_FAILED" in detail or "certificate verify failed" in detail:
            return (
                "TLS verification failed for the model endpoint. Set [tls] ca_bundle to the CA bundle "
                "that signed the gateway's certificate (on RHEL often /etc/pki/tls/certs/ca-bundle.crt)."
            )
        if "certificate required" in detail.lower() or "handshake" in detail.lower():
            return f"TLS handshake failed ({detail}). If the gateway needs a client certificate, set [tls] client_cert and client_key."
        return f"cannot connect to {cfg.base_url}: {detail}. Check llm.base_url and HTTPS_PROXY / NO_PROXY."
    if isinstance(e, openai.APIStatusError):
        return f"HTTP {e.status_code} from the gateway: {text}"
    return text
