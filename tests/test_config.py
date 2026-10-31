from __future__ import annotations

import tomllib

import pytest

from brain.core import config


def write(tmp_path, text: str):
    p = tmp_path / "config.toml"
    p.write_text(text)
    return p


def test_defaults_without_file():
    loaded = config.load()
    assert loaded.path is None
    assert loaded.settings.storage.backend == "local"
    assert loaded.settings.storage.local_root().name == "store"


def test_file_then_env_override(tmp_path, monkeypatch):
    p = write(tmp_path, '[storage]\nbackend = "s3"\nbucket = "from-file"\nprefix = "x"\n[llm]\nembed_dim = 8\n')
    monkeypatch.setenv("BRAIN_STORAGE__BUCKET", "from-env")
    monkeypatch.setenv("BRAIN_API_KEY", "not-a-setting")  # must not be mistaken for config
    loaded = config.load(p)
    s = loaded.settings
    assert s.storage.bucket == "from-env"
    assert s.storage.backend == "s3"  # same section, other key still from the file
    assert s.storage.prefix == "x/"
    assert s.llm.embed_dim == 8
    assert loaded.env_overrides == ["BRAIN_STORAGE__BUCKET"]


def test_brain_config_env_selects_file(tmp_path, monkeypatch):
    p = write(tmp_path, "[threads]\ninactivity_days = 7\n")
    monkeypatch.setenv("BRAIN_CONFIG", str(p))
    assert config.load().settings.threads.inactivity_days == 7


def test_xdg_default_path(monkeypatch, tmp_path):
    p = config.paths.default_config_path()
    p.parent.mkdir(parents=True)
    p.write_text("[server]\nport = 9999\n")
    assert config.load().settings.server.port == 9999


def test_missing_explicit_file_is_an_error(tmp_path):
    with pytest.raises(config.ConfigError, match="not found"):
        config.load(tmp_path / "nope.toml")


@pytest.mark.parametrize(
    "text, fragment",
    [
        ('[storage]\nbackend = "s3"\n', "storage.bucket is required"),
        ('[storage]\nbackend = "ftp"\n', "storage.backend"),
        ('[tls]\nclient_key = "k.pem"\n', "client_cert"),
        ("[llm]\nembed_dimm = 3\n", "llm.embed_dimm"),
        ("[storage\n", "cannot read config"),
    ],
)
def test_invalid_config_messages(tmp_path, text, fragment):
    with pytest.raises(config.ConfigError) as e:
        config.load(write(tmp_path, text))
    assert fragment in str(e.value)


def test_to_toml_roundtrip():
    s = config.Settings()
    s.storage.backend = "s3"
    s.storage.bucket = 'we"ird\\name'
    s.llm.timeout_seconds = 30.5
    text = config.to_toml(s)
    data = tomllib.loads(text)
    assert config.Settings(**data) == s


def test_example_config_is_valid():
    from pathlib import Path

    example = Path(__file__).parents[1] / "examples" / "config.toml"
    s = config.load(example).settings
    assert s.storage.backend == "s3" and s.storage.bucket == "my-team-brain"
