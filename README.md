# brain

A work journal where you only write notes. A model reads each note and keeps
a wiki from them: a page for every piece of work, person, team, and topic,
with where it stands, what was decided, and what is still open. When you come
back to something after weeks away, the page is there, and brain can brief
you on it.

You never file, tag, or review anything. If the wiki gets something wrong,
you write another note saying so ("Jane Doe and J. Doe are the same person",
"the vendor evaluation is finished", "ignore what I wrote about the outage
cause"), and later notes win over earlier ones.

Three front-ends share one core: the CLI, a web UI (`brain start`), and an
MCP server for agents such as Claude Code. Plain files in a local directory
or an S3 bucket are the source of truth: your notes, and the wiki as markdown
you can open in any editor.

What it does with a note:

- Splits it into statements (facts, decisions, questions, things to do,
  ideas) and links them to people, organizations, topics, and threads
  (ongoing pieces of work), creating pages as they are needed.
- Works out what the note changes: a question it answers, a decision it
  replaces, a correction it asks for. Nothing is deleted; a replaced
  statement stays in the page's history, struck through.
- Rewrites the affected pages, and checks every sentence it writes against
  the statements it cites.
- Does this in small steps, each a choice from a list that code checks, so a
  small, cheap model is enough. A stronger model is optional and used only
  for "dreaming": an offline pass that merges duplicates, settles old open
  questions, and closes threads that have gone quiet.
- Writes briefs and answers that cite the dated notes they come from, and
  gives agents a short, dated summary of current state at the start of each
  session.

- [docs/usage.md](docs/usage.md): daily use (writing, correcting, briefs, agents)
- [docs/restricted-network.md](docs/restricted-network.md): setup on a machine with internal mirrors, S3, and a TLS gateway
- [docs/architecture.md](docs/architecture.md): design, file formats, and decisions

## Requirements

- [uv](https://docs.astral.sh/uv/), which installs a managed Python 3.11 or
  newer. That build includes a recent SQLite with FTS5 and allows loading the
  sqlite-vec extension; some system Pythons do not.
- An OpenAI-compatible model endpoint and an API key: a local proxy such as
  LiteLLM, a company gateway, or a provider's own compatible endpoint. A small
  model is enough. It should support tool calling or JSON-schema output;
  `brain doctor` reports which works.
- An embedding model at the same endpoint, strongly recommended. brain uses
  it to find which earlier statements and threads a new note is about.
  Without one, search is keyword-only and a note is compared only with
  statements that share a thread or a name with it.
- Node 22+ and npm only to build the web UI from a clone. A container runtime
  (podman or docker) only for the MinIO tests.

To try every command without a model endpoint, set `BRAIN_FAKE_LLM=1` (see
[Commands](#commands)).

## Install

From a release wheel (the web UI is included; no Node needed):

```sh
uv tool install --managed-python --python 3.12 https://github.com/kvndhrty/second-brain/releases/download/v0.2.0/brain_journal-0.2.0-py3-none-any.whl
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

`small_model` does the work on every note and writes briefs and answers.
`strong_model` is optional; when set, dreaming uses it. For example,
Anthropic's OpenAI-compatible endpoint with Claude Haiku as the small model
and Sonnet for dreaming:

```toml
[llm]
base_url = "https://api.anthropic.com/v1"
api_key_env = "ANTHROPIC_API_KEY"
small_model = "claude-haiku-5-5"
strong_model = "claude-sonnet-5-5"   # optional
embed_model = ""                     # Anthropic serves no embedding model
```

Three more settings choose where the strong model is used: `dream_uses`
(`"strong"` by default), `answer_uses` (`"small"` by default, for `ask` and
`brief`), and `escalate` (`"off"` by default; `"strong"` retries a step on
the strong model when the small model's answer fails its checks).

Chat and embedding calls go to the same `base_url`. To use embeddings with a
provider that has none, put a proxy in front that serves both, such as
LiteLLM routing embeddings to a local model.

A note costs roughly 10 to 25 small model calls, depending on how many
statements and names it has; in a test with Claude Haiku the average was 15,
each taking one to three seconds. Notes are processed in the background, so
writing never waits on this.

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
| `brain jot` | Save a note from `-m TEXT`, `--file`, stdin, or `$EDITOR`, then process it. `--about PAGE` marks it as a correction to that page |
| `brain active` | Threads with recent activity or open items, and where each stands |
| `brain wiki [PAGE]` | Show a wiki page, or list the pages |
| `brain brief REQUEST` | Re-entry brief on a thread, person, org, or topic, or on a request in plain words that may cover several of them |
| `brain ask "QUESTION"` | Answer from the journal, citing dated notes |
| `brain search WORDS` | Find pages, statements, and notes |
| `brain show [NOTE]` | What brain made of a note: its statements, where they were filed, what changed (default: latest) |
| `brain log` | What brain did after each note and in each dream pass |
| `brain dream [--dry-run]` | Dreaming: merge duplicates, settle old open items, close quiet threads, fill in people's roles, rebuild worn summaries |
| `brain context [--format json]` | A short, dated summary of current state for agent sessions |
| `brain process` | Process any pending notes, including markdown files saved into `notes/` by hand |
| `brain rebuild` | Discard the wiki and run every note through the model again, after a prompt or model change |
| `brain sync [--full]` | Bring the local index up to date with the store |
| `brain reindex` | Delete the local index and rebuild it from the store (no model calls) |
| `brain start [--port N] [--open]` | Web UI, HTTP API, MCP over HTTP at `/mcp`, and background processing |
| `brain mcp` | Serve the journal to agents over MCP (stdio) |

To try brain without a model endpoint, set `BRAIN_FAKE_LLM=1`. Every model
call then goes to a scripted fake that turns sentences into statements,
capitalized names into people, `#tags` into topics, and `[[Name]]` into a
thread. The output is not useful as a journal, but it exercises every command.

## The store

```
notes/2026/11/09/20261109T143205Z-vendor-call.md   what you wrote; never changed
wiki/index.md                                      the wiki, as markdown
wiki/threads/…  wiki/people/…  wiki/orgs/…  wiki/topics/…
wiki/log.md                                        what brain changed, newest first
generated/                                         what each step produced; rebuilt by `brain rebuild`
```

The `wiki/` folder can be opened in Obsidian or any editor; links between
pages and to the notes they cite work there. It is overwritten when a page
changes, so edit by writing a note, not by editing the page.

A markdown file saved anywhere under `notes/` becomes a note: `brain start`
picks it up within a minute, and `brain process` at once. Its text is kept
exactly.

## Web UI

`brain start` serves the web UI on http://127.0.0.1:8765 (`[server] port`)
and processes new notes in the background. While it runs, `brain jot` hands
new notes to it instead of processing them in the foreground.

The home page is a box to write a note in, the active threads with where
each stands, and what changed recently. The other pages are the wiki, search
and ask (with "Brief me"), your notes, and the change log. Every wiki page
has a **This is wrong** button: it opens the note box, and what you write
there is saved as a note that corrects that page. Saving a note is the only
thing the UI can change.

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

Tools: `get_context`, `brief`, `ask`, `search`, `get_page`, `get_note`,
`list_active`, and `jot` (writes only when you ask the agent to record
something; a correction is a note too). Every result has today's date and an
age for each dated item.

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
