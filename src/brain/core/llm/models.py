"""Structured model calls (PydanticAI) and embeddings over the shared TLS client.

`LLM.run` sends one prompt file plus a user message and returns a validated
pydantic object. How the structured output is requested depends on what the
gateway supports, set by `llm.structured_output`:

- tool:     the model calls an output tool (most OpenAI-compatible servers)
- native:   `response_format: json_schema`
- prompted: the schema is put in the instructions and the reply parsed as JSON
- auto:     tool, falling back to prompted if the gateway rejects tools
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

import httpx2
import pydantic_ai
from openai import AsyncOpenAI
from pydantic import BaseModel
from pydantic_ai import Agent, ModelHTTPError, ModelRetry, NativeOutput, PromptedOutput, ToolOutput
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from brain.core.config import Settings
from brain.core.formats.records import PromptStamp
from brain.core.llm import client as http
from brain.core.llm.prompts import Prompt, Role, load_prompt
from brain.core.tls import ssl_context

# PydanticAI 2.x prints a banner on first use; it must not appear in CLI output.
pydantic_ai.BANNER_ENABLED = False

T = TypeVar("T", bound=BaseModel)
ModelFactory = Callable[[Prompt, str], Model]
Embedder = Callable[[list[str]], list[list[float]]]

EMBED_BATCH = 64
MAX_EMBED_CHARS = 8000


class LLMError(Exception):
    """A model call failed. The message is meant to be shown to the user."""


class LLMOutputError(LLMError):
    """The model answered, but not with output that passed validation, after retries."""


@dataclass(frozen=True)
class Stamped(Generic[T]):
    output: T
    model: str
    prompt: PromptStamp


def _wrap_output(output_type: type[BaseModel], mode: str) -> Any:
    if mode == "native":
        return NativeOutput(output_type)
    if mode == "prompted":
        return PromptedOutput(output_type)
    return ToolOutput(output_type)


class LLM:
    def __init__(
        self,
        settings: Settings,
        *,
        model_factory: ModelFactory | None = None,
        embedder: Embedder | None = None,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.cfg = settings.llm
        self._factory = model_factory or self._openai_model
        self._embedder = embedder
        self._transport = transport
        self._mode = self.cfg.structured_output
        self._models: dict[tuple[str, str], Model] = {}
        self._embed_client = None

    @property
    def configured(self) -> bool:
        return self.cfg.configured

    @property
    def embed_model(self) -> str:
        return self.cfg.embed_model

    def model_name(self, role: Role) -> str:
        """The configured model for a prompt role. Dreaming and answers use the strong model when asked to and one is set."""
        uses = {"layer": "small", "dream": self.cfg.dream_uses, "answer": self.cfg.answer_uses}[role]
        if uses == "strong" and self.cfg.strong_model:
            return self.cfg.strong_model
        return self.cfg.small_model

    @property
    def can_escalate(self) -> bool:
        return self.cfg.escalate == "strong" and bool(self.cfg.strong_model)

    def _openai_model(self, prompt: Prompt, name: str) -> Model:
        key = self.cfg.api_key()
        if not self.cfg.base_url:
            raise LLMError("llm.base_url is not set; run `brain init` or edit the config")
        if not key:
            raise LLMError(f"the API key environment variable {self.cfg.api_key_env} is not set")
        client = AsyncOpenAI(
            base_url=self.cfg.base_url,
            api_key=key,
            http_client=httpx2.AsyncClient(
                verify=ssl_context(self.settings.tls),
                timeout=httpx2.Timeout(self.cfg.timeout_seconds, connect=15.0),
                transport=self._transport,  # type: ignore[arg-type]
                trust_env=True,
            ),
            max_retries=2,
        )
        return OpenAIChatModel(name, provider=OpenAIProvider(openai_client=client))

    def _model(self, prompt: Prompt, name: str) -> Model:
        k = (prompt.role, name)
        if k not in self._models:
            self._models[k] = self._factory(prompt, name)
        return self._models[k]

    def run(
        self,
        prompt_name: str,
        user: str,
        output_type: type[T],
        *,
        validate: Callable[[T], None] | None = None,
        mode: str | None = None,
        strong: bool = False,
    ) -> Stamped[T]:
        """Run one prompt. `validate` raises ValueError to make the model retry with that message.

        Raises LLMOutputError when the model never produced valid output, and
        LLMError for everything else (no endpoint, HTTP errors).
        """
        prompt = load_prompt(prompt_name)
        name = (self.cfg.strong_model if strong else "") or self.model_name(prompt.role)
        if not name:
            raise LLMError("llm.small_model is not set")
        model = self._model(prompt, name)
        requested = mode or self._mode
        modes = ["tool", "prompted"] if requested == "auto" else [requested]
        last: Exception | None = None
        for i, m in enumerate(modes):
            agent: Agent[None, Any] = Agent(
                model, output_type=_wrap_output(output_type, m), instructions=prompt.instructions, retries=2
            )
            if validate is not None:

                @agent.output_validator
                def _check(out: Any) -> Any:
                    try:
                        validate(out)
                    except ValueError as e:
                        raise ModelRetry(str(e)) from e
                    return out

            try:
                result = agent.run_sync(user)
            except ModelHTTPError as e:
                last = e
                if i + 1 < len(modes) and e.status_code in (400, 404, 422, 501):
                    continue
                raise LLMError(f"{prompt_name}: HTTP {e.status_code} from {name}: {_short(e.body)}") from e
            except UnexpectedModelBehavior as e:
                # The gateway accepted this mode; the output was wrong. Another mode would not help.
                raise LLMOutputError(f"{prompt_name}: {name} did not return valid output: {e}") from e
            except ModelAPIError as e:
                raise LLMError(f"{prompt_name}: {name}: {e}") from e
            if requested == "auto":
                self._mode = m  # remember what worked for the rest of this process
            return Stamped(result.output, name, prompt.stamp)
        raise LLMError(f"{prompt_name}: no structured-output mode worked: {last}")

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        texts = [t[:MAX_EMBED_CHARS] for t in texts]
        if self._embedder is not None:
            return self._embedder(texts)
        if not self.cfg.embed_model:
            raise LLMError("llm.embed_model is not set")
        if self._embed_client is None:
            try:
                self._embed_client = http.openai_client(self.settings, transport=self._transport)
            except http.LLMConfigError as e:
                raise LLMError(str(e)) from e
        out: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH):
            try:
                out += http.embed(self._embed_client, self.cfg, texts[start : start + EMBED_BATCH]).vectors
            except Exception as e:  # noqa: BLE001 - reported with a fix
                raise LLMError(f"embedding failed: {http.explain_error(e, self.cfg)}") from e
        return out


def _short(body: object) -> str:
    text = str(body)
    return text if len(text) < 300 else text[:300] + "…"
