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
    chat_model: str = ""
    extract_model: str = ""
    embed_model: str = ""
    doc_model: str = ""
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
