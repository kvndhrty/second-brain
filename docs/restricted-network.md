# Setting up brain on a restricted machine

This is the checklist for a machine with internal package mirrors, an S3
bucket, and an OpenAI-compatible gateway that may require a corporate CA and a
client certificate. The examples use RHEL 9, but nothing in it is RHEL-specific
except the CA bundle path.

## 1. Get uv and a Python

uv is on PyPI, so it installs from the internal mirror:

```sh
python3 -m pip install --user uv          # RHEL's python3 is fine for this; needs the python3-pip package
export UV_DEFAULT_INDEX=https://pypi-mirror.internal/simple
export UV_PYTHON_INSTALL_MIRROR=https://uv-mirror.internal/python-build-standalone
uv python install 3.12
```

If uv rejects the mirror's certificate, set `UV_NATIVE_TLS=1` so it uses the
OS trust store, which on RHEL includes certificates added with
`update-ca-trust`.

Use uv's Python, not RHEL's `python3.11` package: brain needs a recent SQLite
with extension loading for sqlite-vec. `brain doctor` fails clearly if the
Python is wrong.

## 2. Install brain

Either from a release wheel, which already contains the web UI:

```sh
uv tool install --managed-python --python 3.12 \
  https://github.com/kvndhrty/second-brain/releases/download/vX.Y.Z/brain_journal-X.Y.Z-py3-none-any.whl
```

or from a clone, which builds the web UI during install and needs Node 22+
and the npm mirror (`npm config set registry https://npm-mirror.internal/`):

```sh
git clone https://github.com/kvndhrty/second-brain.git
cd second-brain
uv tool install --managed-python --python 3.12 .
```

Without Node, install from the clone with `BRAIN_SKIP_FRONTEND=1`. Everything
except the web UI works; `brain start` then serves the API and MCP only.

## 3. Configure

Run `brain init`, or write `~/.config/brain/config.toml` by hand:

```toml
[storage]
backend = "s3"
bucket = "my-brain-bucket"
prefix = "brain/"
endpoint_url = ""          # set for an S3-compatible store
addressing_style = "auto"  # "path" for most S3-compatible stores
region = ""

[tls]                      # used for both S3 and the model gateway
ca_bundle = "/etc/pki/tls/certs/ca-bundle.crt"
client_cert = ""           # set both for mTLS
client_key = ""

[llm]
base_url = "https://llm-gateway.internal/v1"
api_key_env = "BRAIN_API_KEY"
small_model = "..."         # does the work on every note
strong_model = ""           # optional; used for dreaming
embed_model = "..."
```

Secrets stay in the environment:

```sh
export BRAIN_API_KEY=...            # or whatever api_key_env names
export AWS_PROFILE=...              # or AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
```

## 4. Run `brain doctor`

Every check prints what it found and, when something is wrong, what to change.
The common problems on a restricted network:

| Check | Symptom | Fix |
|---|---|---|
| sqlite / sqlite-vec | "cannot load extensions" | Reinstall with `uv tool install --managed-python --python 3.12 ...` (uv's Python) |
| index path | "on a network filesystem (nfs4)" | Home is on NFS. Set `[index] path = "/var/tmp/$USER/brain/index.db"` |
| tls | "TLS file not found" or key mismatch | Check the paths in `[tls]`; the key must belong to the certificate |
| storage | "no AWS credentials found" | Set `AWS_PROFILE` or the key variables, in the same shell |
| storage | "CERTIFICATE_VERIFY_FAILED" | Set `[tls] ca_bundle` to the bundle that signed the S3 endpoint |
| conditional writes | "store ignored If-None-Match" | The store does not enforce conditional writes. brain falls back to writing and reading back. Fine for one machine at a time |
| proxy | "localhost would go through proxy" | Add the host to `NO_PROXY` |
| models | "gateway rejected the API key (401)" | Check the variable named by `llm.api_key_env` |
| models | "cannot list models" (warning) | Some gateways do not implement `/models`; the per-model checks below still run |
| structured output | "tool fails, … prompted works" | Set `llm.structured_output` to the mode doctor names |
| embedding | "returned N dimensions but llm.embed_dim = M" | Set `embed_dim = 0` (detect) or the right number |
| lease | "held by another machine" | That machine is using the store. Stop it there, or wait for the lease to expire (15 minutes), or use `--force` if it is gone |

FIPS mode is reported for information; brain uses only SHA-256, so it works
with FIPS enabled.

## 5. Try the real models once

```sh
BRAIN_LIVE_TESTS=1 BRAIN_TEST_LIVE_CONFIG=~/.config/brain/config.toml \
  uv run --managed-python --python 3.12 pytest tests/live        # from a clone
```

This processes one sample note into a throwaway local store and asks a
question about it. If the results look wrong, the prompts are in
`src/brain/prompts/`, one file per step; after changing them, `brain rebuild`
runs every note again.

## 6. Daily use

```sh
brain start            # web UI on http://127.0.0.1:8765, background processing
brain jot              # or write in the web UI
brain wiki             # the pages brain keeps from your notes
brain brief "thread or person"
```

If the machine is remote, forward the port instead of exposing it:
`ssh -L 8765:127.0.0.1:8765 remote-host`, then open http://127.0.0.1:8765.

Register brain with Claude Code once, either over HTTP while `brain start`
runs, or over stdio:

```sh
claude mcp add --scope user --transport http brain http://127.0.0.1:8765/mcp
# or
claude mcp add --scope user brain -e BRAIN_API_KEY="$BRAIN_API_KEY" -- brain mcp
```

## Moving to another machine

The store is the only state that matters; the index on each machine is
rebuilt from it. On the new machine, install, copy the config, run
`brain doctor`, then any command. The first one builds the index without
calling the models. Only one machine writes at a time: the lease is released
when brain exits and expires 15 minutes after the last write otherwise.
