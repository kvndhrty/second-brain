# brain

A work journal that an LLM turns into a linked, searchable knowledge graph.
You write a few paragraphs a day; brain extracts decisions, questions, people,
themes, and workstreams, and can brief you on any of them when you come back
after weeks away.

Three front-ends share one core: the CLI, a web UI (`brain start`), and an MCP
server for agents such as Claude Code. Plain files in a local directory or an
S3 bucket are the source of truth; the search index is rebuilt from them.

What it does with an entry:

- Splits it into fragments (decisions, questions, action items, claims,
  ideas) and links them to people, organizations, themes, and threads
  (workstreams). New people and organizations wait for you to confirm them.
- Notices when a later entry seems to answer an earlier open question, and
  asks you before closing it.
- Writes briefs and answers that cite the dated entries they come from. Every
  note given to the model carries its date and age, so newer information wins.
- Runs an offline "dream" pass that rebuilds thread summaries, proposes items
  that later entries replaced, and writes a one-line status per thread.
- Gives agents a short, dated summary of current state at the start of each
  session.

- [docs/usage.md](docs/usage.md): daily use (writing, review, briefs, keeping things current)
- [docs/restricted-network.md](docs/restricted-network.md): setup on a machine with internal mirrors, S3, and a TLS gateway
- [docs/architecture.md](docs/architecture.md): design, file formats, and decisions

## Requirements

- [uv](https://docs.astral.sh/uv/), which installs a managed Python 3.11 or
  newer. That build includes a recent SQLite with FTS5 and allows loading the
  sqlite-vec extension; some system Pythons do not.
- An OpenAI-compatible model endpoint and an API key: a local proxy such as
  LiteLLM, a company gateway, or a provider's own compatible endpoint. The
  chat model should support tool calling or JSON-schema output.
- Optionally, an embedding model at the same endpoint. Without one, search is
  keyword-only and new entries are not checked against earlier open questions
  until `brain dream` runs.
- Node 22+ and npm only to build the web UI from a clone. A container runtime
  (podman or docker) only for the MinIO tests.

To try every command without a model endpoint, set `BRAIN_FAKE_LLM=1` (see
[Commands](#commands)).

## Install

From a release wheel (the web UI is included; no Node needed):

```sh
uv tool install --managed-python --python 3.12 https://github.com/kvndhrty/second-brain/releases/download/v0.1.0/brain_journal-0.1.0-py3-none-any.whl
```

From a clone (the web UI is built during install, which needs Node 22+ and npm;
set `BRAIN_SKIP_FRONTEND=1` to install without the UI):

```sh
git clone https://github.com/kvndhrty/second-brain.git
cd second-brain
uv tool install --managed-python --python 3.12 .
```

Then:

```sh
brain init
brain doctor
```

In an environment with internal mirrors, point uv at them before installing:

```sh
export UV_DEFAULT_INDEX=https://pypi-mirror.internal/simple
export UV_PYTHON_INSTALL_MIRROR=https://uv-mirror.internal/python-build-standalone
# If uv rejects the mirror's certificate, have it use the OS trust store:
export UV_NATIVE_TLS=1
# For building the web UI from a clone:
npm config set registry https://npm-mirror.internal/
```

## Configure

`brain init` writes `~/.config/brain/config.toml`. A full example with every
key is in [examples/config.toml](examples/config.toml). `--config PATH` or
`BRAIN_CONFIG` selects another file, and `BRAIN_<SECTION>__<KEY>` env vars
override single values.

The API key is never stored in the config. `llm.api_key_env` names the
environment variable that holds it (default `BRAIN_API_KEY`).

### Models

`chat_model` writes briefs, answers, summaries, and dream output;
`extract_model` does extraction, name resolution, and routing, and can be a
smaller model. For example, Anthropic's OpenAI-compatible endpoint with Claude
Haiku for both:

```toml
[llm]
base_url = "https://api.anthropic.com/v1"
api_key_env = "ANTHROPIC_API_KEY"
chat_model = "claude-haiku-5-5"
extract_model = "claude-haiku-5-5"
embed_model = ""                  # Anthropic serves no embedding model
```

Chat and embedding calls go to the same `base_url`. To use embeddings with a
provider that has none, put a proxy in front that serves both, such as
LiteLLM routing embeddings to a local model.

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
| `brain review` | Confirm new people and orgs, proposed themes and threads, proposed answers, and items dreaming thinks are overcome by events |
| `brain active` | Threads with recent activity, plus pinned ones |
| `brain brief TARGET` | Re-entry brief on a thread, person, org, or theme, or on a request in plain words that may cover several of them |
| `brain ask "QUESTION"` | Answer from the journal, citing dated entries |
| `brain show [ENTRY]` | An entry's fragments with ids and status (default: latest) |
| `brain status FRAGMENT --obe/--speculative/--confirmed/--closed/--open/--someday` | Change a fragment's status |
| `brain thread list/show/rename/pin/unpin/archive/activate/obe/merge/split/move` | Manage threads |
| `brain sweep` | Go through stale open items, unconfirmed speculation, and quiet threads |
| `brain dream [--dry-run] [--thread ID]` | Offline consolidation: rebuild stale summaries, propose superseded and answered items, check the sweep, write each thread's Now line |
| `brain context [--format json]` | A short, dated summary of current state for agent sessions |
| `brain process` | Run any pending extraction and stale thread summaries |
| `brain reextract [IDS] [--since D] [--stale-prompts]` | Re-run extraction after a model or prompt change; your decisions are kept |
| `brain sync [--full]` | Bring the local index up to date with the store |
| `brain reindex` | Delete the local index and rebuild it from the store (no model calls) |
| `brain start [--port N] [--open]` | Web UI, HTTP API, MCP over HTTP at `/mcp`, and background processing |
| `brain mcp` | Serve the journal to agents over MCP (stdio) |

To try brain without a model endpoint, set `BRAIN_FAKE_LLM=1`. Every model
call then goes to a scripted fake that turns sentences into fragments,
capitalized names into people, `#tags` into themes, and `[[Name]]` into a
thread. The output is not useful as a journal, but it exercises every command.

## Web UI

`brain start` serves the web UI on http://127.0.0.1:8765 (`[server] port`)
and processes new entries in the background. While it runs, `brain jot` hands
new entries to it instead of processing them in the foreground. Pages: active
threads, thread/person/org/theme pages with a filterable timeline, "Brief me",
and thread actions (pin, archive, overcome by events, rename, merge, split);
review queue; search and ask; entries with an editor; someday ideas; "Tidy up" (the
overcome-by-events sweep); and a graph of how threads, people, orgs, and
themes connect.

It listens on localhost only and has no login. It rejects requests whose Host
header is not localhost and writes that are not JSON, which stops other
websites open in your browser from reading or changing it.

## Use from Claude Code

`brain mcp` is an MCP server on stdio. Register it once:

```sh
claude mcp add --scope user brain -- brain mcp
```

If `brain start` is running, Claude Code can use it over HTTP instead, sharing
its background processing:

```sh
claude mcp add --scope user --transport http brain http://127.0.0.1:8765/mcp
```

With the stdio form, Claude Code starts the server with its own environment. If the API key or AWS
credentials are only set in your interactive shell, pass them explicitly, and
point at a config file if it is not in the default place:

```sh
claude mcp add --scope user brain \
  -e BRAIN_CONFIG=$HOME/.config/brain/config.toml \
  -e BRAIN_API_KEY="$BRAIN_API_KEY" \
  -- brain mcp
```

Tools: `search`, `ask`, `brief`, `get_entity`, `get_entry`, `list_active`,
`get_context`, and `add_entry` (writes only when you ask the agent to record
something). Every result has today's date and an age for each dated item.

`brain mcp` puts a short summary of current state (`brain context`) in its
instructions, so the agent starts each session knowing the active threads and
open items; it is also the resource `brain://context`. With the HTTP form, or
for other agents, a Claude Code `SessionStart` hook that runs `brain context`
does the same; see [docs/usage.md](docs/usage.md#agents).

## Development

```sh
uv sync
uv run pytest            # MinIO tests run if podman or docker is available
ci/lint.sh
ci/frontend.sh           # typecheck, test, and build the web UI
```

Web UI development: run `brain start` (or `BRAIN_FAKE_LLM=1 brain start`) and
`npm run dev` in `frontend/`; see [frontend/README.md](frontend/README.md).

Releases: pushing a tag `vX.Y.Z` runs `.github/workflows/release.yml`, which
builds the wheel with the UI included and attaches it to a GitHub release.

- S3 contract tests run against moto (in-process) always, and against MinIO
  when a container runtime is available or `BRAIN_TEST_S3_ENDPOINT` is set.
- Live model tests: `BRAIN_LIVE_TESTS=1 BRAIN_TEST_LIVE_CONFIG=path uv run pytest tests/live`.
- CI runs on GitHub Actions through `ci/*.sh`, which GitLab CI can call
  unchanged. One job runs the suite in Red Hat's UBI 9 image.

## License

Apache License 2.0. See [LICENSE](LICENSE).
