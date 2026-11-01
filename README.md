# brain

A work journal that an LLM turns into a linked, searchable knowledge graph.
You write a few paragraphs a day; brain extracts decisions, questions, people,
themes, and workstreams, and can brief you on any of them when you come back
after weeks away.

Status: milestone 3 of 5. The CLI works end to end: entries are extracted into
fragments, people, orgs, themes, and threads; you confirm new names in
`brain review`; `ask`, `brief`, and `active` answer from the graph with
citations. `brain mcp` serves the same to agents such as Claude Code. The web
UI (milestone 4) comes next.
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

## Commands

| Command | What it does |
|---|---|
| `brain init` | Write a config file interactively |
| `brain doctor` | Check config, storage, lease, TLS, proxies, model endpoints, structured output, and the index |
| `brain jot` | Save an entry from `-m TEXT`, `--file`, stdin, or `$EDITOR`, then process it |
| `brain review` | Confirm new people and orgs, proposed themes and threads, and proposed answers |
| `brain active` | Threads with recent activity, plus pinned ones |
| `brain brief TARGET` | Re-entry brief on a thread, person, org, theme, or free text |
| `brain ask "QUESTION"` | Answer from the journal, citing dated entries |
| `brain show [ENTRY]` | An entry's fragments with ids and status (default: latest) |
| `brain status FRAGMENT --obe/--speculative/--confirmed/--closed/--open/--dream` | Change a fragment's status |
| `brain process` | Run any pending extraction and stale thread summaries |
| `brain reextract [IDS] [--since D] [--stale-prompts]` | Re-run extraction after a model or prompt change; your decisions are kept |
| `brain sync [--full]` | Bring the local index up to date with the store |
| `brain reindex` | Delete the local index and rebuild it from the store (no model calls) |
| `brain mcp` | Serve the journal to agents over MCP (stdio) |

To try brain without a model endpoint, set `BRAIN_FAKE_LLM=1`. Every model
call then goes to a scripted fake that turns sentences into fragments,
capitalized names into people, `#tags` into themes, and `[[Name]]` into a
thread. The output is not useful as a journal, but it exercises every command.

## Use from Claude Code

`brain mcp` is an MCP server on stdio. Register it once:

```sh
claude mcp add --scope user brain -- brain mcp
```

Claude Code starts the server with its own environment. If the API key or AWS
credentials are only set in your interactive shell, pass them explicitly, and
point at a config file if it is not in the default place:

```sh
claude mcp add --scope user brain \
  -e BRAIN_CONFIG=$HOME/.config/brain/config.toml \
  -e BRAIN_API_KEY="$BRAIN_API_KEY" \
  -- brain mcp
```

Tools: `search`, `ask`, `brief`, `get_entity`, `get_entry`, `list_active`, and
`add_entry` (writes only when you ask the agent to record something).

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
