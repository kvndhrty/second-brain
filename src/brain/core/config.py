"""Settings: defaults, then one TOML file, then BRAIN_<SECTION>__<KEY> env vars."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from brain.core import paths

ENV_PREFIX = "BRAIN_"
CONFIG_ENV = "BRAIN_CONFIG"


class ConfigError(Exception):
    """The config file is missing, unreadable, or invalid. The message says which."""


class _Section(BaseModel):
    # Unknown keys are errors, so a typo in config.toml is reported instead of ignored.
    model_config = ConfigDict(extra="forbid")


class StorageConfig(_Section):
    backend: Literal["local", "s3"] = "local"
    bucket: str = ""
    prefix: str = "brain/"
    endpoint_url: str = ""
    region: str = ""
    profile: str = ""
    addressing_style: Literal["auto", "path", "virtual"] = "auto"
    path: str = ""

    @model_validator(mode="after")
    def _check(self) -> StorageConfig:
        if self.backend == "s3" and not self.bucket:
            raise ValueError("storage.bucket is required when storage.backend = 's3'")
        if self.prefix and not self.prefix.endswith("/"):
            self.prefix += "/"
        self.prefix = self.prefix.lstrip("/")
        return self

    def local_root(self) -> Path:
        return Path(self.path).expanduser() if self.path else paths.data_dir() / "store"


class TLSConfig(_Section):
    ca_bundle: str = ""
    client_cert: str = ""
    client_key: str = ""

    @model_validator(mode="after")
    def _check(self) -> TLSConfig:
        if self.client_key and not self.client_cert:
            raise ValueError("tls.client_key is set but tls.client_cert is not")
        return self


class LLMConfig(_Section):
    base_url: str = ""
    api_key_env: str = "BRAIN_API_KEY"
    small_model: str = ""  # layers 1-8 and brief routing
    strong_model: str = ""  # optional
    embed_model: str = ""
    dream_uses: Literal["strong", "small"] = "strong"  # falls back to small when no strong model is set
    answer_uses: Literal["strong", "small"] = "small"  # ask and brief answers
    # Retry on the strong model when the small model's output fails validation.
    escalate: Literal["off", "strong"] = "off"
    embed_dim: int = Field(default=0, ge=0)
    structured_output: Literal["auto", "tool", "native", "prompted"] = "auto"
    timeout_seconds: float = Field(default=120, gt=0)

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) or None


class IndexConfig(_Section):
    path: str = ""


class ThreadsConfig(_Section):
    inactivity_days: int = Field(default=21, ge=1)


class LeaseConfig(_Section):
    ttl_seconds: int = Field(default=900, ge=30)


class ServerConfig(_Section):
    port: int = Field(default=8765, ge=1, le=65535)


class WorkerConfig(_Section):
    poll_seconds: int = Field(default=60, ge=5)


class DreamConfig(_Section):
    """Offline consolidation (`brain dream`, and on a schedule inside `brain start`)."""

    enabled: bool = True  # whether `brain start` dreams on its own; `brain dream` always works
    nightly_at: str = "03:00"  # local time HH:MM; "" turns the nightly pass off
    after_notes: int = Field(default=10, ge=0)  # also dream after this many new notes; 0 turns it off
    max_calls: int = Field(default=50, ge=1)  # model calls per pass
    rebuild_after_updates: int = Field(default=10, ge=1)  # rebuild a summary after this many updates
    stale_days: int = Field(default=14, ge=1)  # re-check open questions and actions older than this
    close_after_days: int = Field(
        default=60, ge=1
    )  # close a thread with no open items after this long without activity
    rebuild_after_days: int = Field(default=30, ge=1)  # or when it was last rebuilt this long ago

    @model_validator(mode="after")
    def _check(self) -> DreamConfig:
        if self.nightly_at:
            hh, _, mm = self.nightly_at.partition(":")
            if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60):
                raise ValueError('dream.nightly_at must be "HH:MM" (local time) or ""')
        return self

    def nightly_time(self) -> tuple[int, int] | None:
        if not self.nightly_at:
            return None
        hh, _, mm = self.nightly_at.partition(":")
        return int(hh), int(mm)


class ContextConfig(_Section):
    """The injected context (`brain context`): a short summary of current state for agent sessions."""

    max_lines: int = Field(default=60, ge=10)
    max_bytes: int = Field(default=6000, ge=500)
    recent_days: int = Field(default=7, ge=1)  # "changed in the last N days"
    open_items: int = Field(default=5, ge=0)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        extra="forbid",
    )

    storage: StorageConfig = StorageConfig()
    tls: TLSConfig = TLSConfig()
    llm: LLMConfig = LLMConfig()
    index: IndexConfig = IndexConfig()
    threads: ThreadsConfig = ThreadsConfig()
    lease: LeaseConfig = LeaseConfig()
    server: ServerConfig = ServerConfig()
    worker: WorkerConfig = WorkerConfig()
    dream: DreamConfig = DreamConfig()
    context: ContextConfig = ContextConfig()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # File values arrive as init kwargs; env vars must win over them.
        return (env_settings, init_settings)


class LoadedSettings(BaseModel):
    """Settings plus where they came from, for `brain doctor`."""

    settings: Settings
    path: Path | None
    env_overrides: list[str]


def resolve_config_path(explicit: str | Path | None) -> tuple[Path, bool]:
    """Return (path, explicitly_requested)."""
    if explicit:
        return Path(explicit).expanduser(), True
    if os.environ.get(CONFIG_ENV):
        return Path(os.environ[CONFIG_ENV]).expanduser(), True
    return paths.default_config_path(), False


def _env_overrides() -> list[str]:
    sections = set(Settings.model_fields)
    found = []
    for name in sorted(os.environ):
        if not name.startswith(ENV_PREFIX):
            continue
        section = name[len(ENV_PREFIX) :].split("__", 1)[0].lower()
        if section in sections:
            found.append(name)
    return found


def load(explicit: str | Path | None = None) -> LoadedSettings:
    path, explicit_request = resolve_config_path(explicit)
    data: dict[str, Any] = {}
    used_path: Path | None = None
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ConfigError(f"cannot read config {path}: {e}") from e
        used_path = path
    elif explicit_request:
        raise ConfigError(f"config file not found: {path}")
    try:
        settings = Settings(**data)
    except ValidationError as e:
        where = f"config {used_path}" if used_path else "configuration"
        raise ConfigError(f"invalid {where}:\n{_format_validation(e)}") from e
    return LoadedSettings(settings=settings, path=used_path, env_overrides=_env_overrides())


def _format_validation(e: ValidationError) -> str:
    lines = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(top level)"
        lines.append(f"  {loc}: {err['msg']}")
    return "\n".join(lines)


def to_toml(settings: Settings) -> str:
    """Serialize settings as TOML. Values are only str, int, float, and bool."""
    out = []
    for section, model in settings:
        out.append(f"[{section}]")
        for key, value in model.model_dump().items():
            out.append(f"{key} = {_toml_value(value)}")
        out.append("")
    return "\n".join(out)


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return repr(v)
    s = str(v)
    escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{escaped}"'
