# brain

A work journal that an LLM turns into a linked, searchable knowledge graph.
You write a few paragraphs a day; brain extracts decisions, questions, people,
themes, and workstreams, and can brief you on any of them when you come back
after weeks away.

Status: milestone 1 of 5. Storage, config, the index, `jot`, `reindex`,
`doctor`, and `init` work. Extraction, `ask`, and `brief` arrive in milestone 2.
See [docs/architecture.md](docs/architecture.md) for the design.

## Install

brain needs a uv-managed Python (3.11 or newer). That build includes a recent
SQLite with FTS5 and allows loading the sqlite-vec extension; some system
Pythons do not.

```sh
git clone https://github.com/<you>/second-brain.git
cd second-brain
uv tool install --python 3.12 .
brain init
brain doctor
```

In an environment with internal mirrors, point uv at them before installing:

```sh
export UV_DEFAULT_INDEX=https://pypi-mirror.internal/simple
export UV_PYTHON_INSTALL_MIRROR=https://uv-mirror.internal/python-build-standalone
# If uv rejects the mirror's certificate, have it use the OS trust store:
export UV_NATIVE_TLS=1
```

## Configure

`brain init` writes `~/.config/brain/config.toml`. A full example with every
key is in [examples/config.toml](examples/config.toml). `--config PATH` or
`BRAIN_CONFIG` selects another file, and `BRAIN_<SECTION>__<KEY>` env vars
override single values.

The API key is never stored in the config. `llm.api_key_env` names the
environment variable that holds it (default `BRAIN_API_KEY`).

### S3 bucket + local proxy

```toml
[storage]
backend = "s3"
bucket = "team-brain"
prefix = "brain/"

[llm]
base_url = "http://127.0.0.1:4000/v1"
```

AWS credentials come from the standard chain (`AWS_PROFILE`,
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, instance roles). For an
S3-compatible store, also set `endpoint_url` and usually
`addressing_style = "path"`.

### Local directory + local proxy

```toml
[storage]
backend = "local"
path = "~/brain-store"

[llm]
base_url = "http://127.0.0.1:4000/v1"
```

### Either storage + remote gateway with mTLS

```toml
[tls]
ca_bundle = "/etc/pki/tls/certs/ca-bundle.crt"
client_cert = "~/.certs/me.crt"
client_key = "~/.certs/me.key"

[llm]
base_url = "https://llm-gateway.internal/v1"
```

The `[tls]` settings apply to both the gateway and S3. With `ca_bundle` empty,
brain uses the OS trust store.

## Commands (milestone 1)

| Command | What it does |
|---|---|
| `brain init` | Write a config file interactively |
| `brain doctor` | Check config, storage, lease, TLS, proxies, model endpoints, and the index |
| `brain jot` | Save an entry from `-m TEXT`, `--file`, stdin, or `$EDITOR` |
| `brain sync [--full]` | Bring the local index up to date with the store |
| `brain reindex` | Delete the local index and rebuild it from the store (no model calls) |

## Development

```sh
uv sync
uv run pytest            # MinIO tests run if podman or docker is available
ci/lint.sh
```

- S3 contract tests run against moto (in-process) always, and against MinIO
  when a container runtime is available or `BRAIN_TEST_S3_ENDPOINT` is set.
- Live model tests: `BRAIN_LIVE_TESTS=1 BRAIN_TEST_LIVE_CONFIG=path uv run pytest tests/live`.
- CI runs on GitHub Actions through `ci/*.sh`, which GitLab CI can call
  unchanged. One job runs the suite in Red Hat's UBI 9 image.
