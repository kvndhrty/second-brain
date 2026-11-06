"""`brain doctor`: checks that the configuration works on this machine.

Each check returns a status and, when something is wrong, a sentence saying
what to change. Checks never raise; a failure in one does not stop the rest
unless later checks depend on it.
"""

from __future__ import annotations

import os
import secrets
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx2
import openai

from brain.core import keys
from brain.core.config import ConfigError, LoadedSettings, Settings, load
from brain.core.index import db as index_db
from brain.core.index.sync import read_generation
from brain.core.localstate import LocalState
from brain.core.storage import Storage, StorageError, open_storage
from brain.core.storage.lease import read_lease
from brain.core.storage.probe import probe_conditional_writes
from brain.core.tls import TLSConfigError, describe_trust, ssl_context

Status = Literal["ok", "warn", "fail", "skip", "info"]
NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs", "9p", "afs"}


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str
    fix: str | None = None


class Doctor:
    def __init__(
        self,
        config: str | None = None,
        *,
        storage: Storage | None = None,
        state: LocalState | None = None,
        llm_transport: httpx2.BaseTransport | None = None,
        loaded: LoadedSettings | None = None,
    ) -> None:
        self.config = config
        self._storage = storage
        self.state = state or LocalState()
        self.llm_transport = llm_transport
        self._loaded = loaded
        self.results: list[CheckResult] = []

    def add(self, name: str, status: Status, detail: str, fix: str | None = None) -> None:
        self.results.append(CheckResult(name, status, detail, fix))

    def run(self) -> list[CheckResult]:
        settings = self.check_config()
        self.check_python()
        if settings is None:
            return self.results
        self.check_index_path(settings)
        self.check_tls(settings)
        self.check_proxy(settings)
        storage = self.check_storage(settings)
        if storage is not None:
            self.check_conditional_writes(storage)
            self.check_lease(storage)
        self.check_llm(settings)
        if storage is not None:
            self.check_index(settings, storage)
        return self.results

    # --- config and runtime ----------------------------------------------------

    def check_config(self) -> Settings | None:
        try:
            loaded = self._loaded or load(self.config)
        except ConfigError as e:
            self.add("config", "fail", str(e), "Fix the file, or run `brain init` to write a new one.")
            return None
        env = f"; env overrides: {', '.join(loaded.env_overrides)}" if loaded.env_overrides else ""
        if loaded.path is None:
            self.add("config", "warn", f"no config file found; using defaults{env}", "Run `brain init` to create one.")
        else:
            self.add("config", "ok", f"loaded {loaded.path}{env}")
        s = loaded.settings
        where = (
            s.storage.local_root() if s.storage.backend == "local" else f"s3://{s.storage.bucket}/{s.storage.prefix}"
        )
        self.add("config", "info", f"storage: {s.storage.backend} ({where})")
        return s

    def check_python(self) -> None:
        v = sys.version_info
        if v < (3, 11):
            self.add("python", "fail", f"Python {v.major}.{v.minor} is too old", "Install with Python 3.11 or newer.")
        else:
            self.add("python", "ok", f"Python {v.major}.{v.minor}.{v.micro} ({sys.executable})")
        caps = index_db.capabilities()
        min_v = ".".join(map(str, index_db.MIN_SQLITE))
        if not caps.sqlite_ok:
            self.add(
                "sqlite",
                "fail",
                f"SQLite {caps.sqlite_version} is older than {min_v}",
                "Install brain with a uv-managed Python: `uv tool install --managed-python --python 3.12 .`",
            )
        else:
            self.add("sqlite", "ok", f"SQLite {caps.sqlite_version}")
        if not caps.fts5:
            self.add("sqlite", "fail", "SQLite was built without FTS5", "Use a uv-managed Python build.")
        if caps.vec_version:
            self.add("sqlite-vec", "ok", f"sqlite-vec {caps.vec_version} loads")
        else:
            self.add(
                "sqlite-vec",
                "fail",
                f"sqlite-vec does not load: {caps.vec_error}",
                "Install brain with a uv-managed Python: `uv tool install --managed-python --python 3.12 .`",
            )
        fips = Path("/proc/sys/crypto/fips_enabled")
        if fips.exists():
            on = fips.read_text().strip() == "1"
            self.add("fips", "info", "FIPS mode is on (brain uses only SHA-256)" if on else "FIPS mode is off")

    def check_index_path(self, settings: Settings) -> None:
        from brain.core.services import index_path

        try:
            storage = self._storage or open_storage(settings)
            path = index_path(settings, storage)
        except Exception as e:  # noqa: BLE001
            self.add("index path", "skip", f"cannot determine the index path: {e}")
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            probe = path.parent / f".write-test-{secrets.token_hex(4)}"
            probe.write_text("x")
            probe.unlink()
        except OSError as e:
            self.add(
                "index path",
                "fail",
                f"{path.parent} is not writable: {e}",
                "Set [index] path to a local, writable directory.",
            )
            return
        fs = _filesystem_type(path.parent)
        if fs in NETWORK_FS:
            self.add(
                "index path",
                "warn",
                f"{path} is on a network filesystem ({fs}); SQLite locking is unreliable there",
                "Set [index] path to a local disk, e.g. /var/tmp/$USER/brain/index.db.",
            )
        else:
            self.add("index path", "ok", str(path) + (f" ({fs})" if fs else ""))

    def check_tls(self, settings: Settings) -> None:
        t = settings.tls
        try:
            ssl_context(t)
        except TLSConfigError as e:
            self.add("tls", "fail", str(e), "Check the paths in [tls]; the key must match the certificate.")
            return
        parts = [f"trust: {describe_trust(t)}"]
        if t.client_cert:
            parts.append(f"client certificate: {t.client_cert}")
        self.add("tls", "ok", "; ".join(parts))

    def check_proxy(self, settings: Settings) -> None:
        proxies = urllib.request.getproxies_environment()
        urls = []
        if settings.llm.base_url:
            urls.append(("model endpoint", settings.llm.base_url))
        if settings.storage.backend == "s3":
            urls.append(("S3 endpoint", settings.storage.endpoint_url or "https://s3.amazonaws.com"))
        for label, url in urls:
            parsed = urlparse(url)
            host = parsed.hostname or ""
            scheme = parsed.scheme or "https"
            proxy = proxies.get(scheme) or proxies.get("all")
            # Undocumented but stable since Python 3.x; it applies NO_PROXY the way httpx2 does.
            bypass = bool(proxy) and urllib.request.proxy_bypass_environment(host, proxies)  # pyright: ignore[reportAttributeAccessIssue]
            if not proxy or bypass:
                self.add("proxy", "ok", f"{label} {host}: direct connection")
            elif host in ("localhost", "127.0.0.1", "::1"):
                self.add("proxy", "warn", f"{label} {host} would go through proxy {proxy}", f"Add {host} to NO_PROXY.")
            else:
                self.add("proxy", "info", f"{label} {host}: via proxy {proxy}")

    # --- storage -----------------------------------------------------------------

    def check_storage(self, settings: Settings) -> Storage | None:
        try:
            storage = self._storage or open_storage(settings)
        except Exception as e:  # noqa: BLE001
            self.add("storage", "fail", f"cannot open storage: {e}")
            return None
        key = f"{keys.DOCTOR_PREFIX}{self.state.machine_id[:8]}-{secrets.token_hex(4)}"
        payload = f"brain doctor {datetime.now(UTC).isoformat()}".encode()
        step = "put"
        try:
            storage.put(key, payload)
            step = "get"
            if storage.get(key) != payload:
                raise StorageError("read back different bytes than were written")
            step = "list"
            if key not in {o.key for o in storage.list(keys.DOCTOR_PREFIX)}:
                raise StorageError("object not visible in listing right after writing it")
            step = "delete"
            storage.delete(key)
            if storage.exists(key):
                raise StorageError("object still exists after delete")
        except Exception as e:  # noqa: BLE001
            self.add("storage", "fail", f"{storage.describe()}: {step} failed: {e}")
            return None
        self.add("storage", "ok", f"{storage.describe()}: put/get/list/delete work")
        return storage

    def check_conditional_writes(self, storage: Storage) -> None:
        try:
            result = probe_conditional_writes(storage)
        except Exception as e:  # noqa: BLE001
            self.add("conditional writes", "warn", f"probe failed: {e}")
            return
        if result.enforced:
            self.add("conditional writes", "ok", result.detail)
        else:
            self.add(
                "conditional writes",
                "warn",
                result.detail + "; the lease will use read-back verification",
                "This works for one machine at a time but cannot fully prevent two simultaneous writers.",
            )

    def check_lease(self, storage: Storage) -> None:
        try:
            current = read_lease(storage)
        except Exception as e:  # noqa: BLE001
            self.add("lease", "warn", f"cannot read lease: {e}")
            return
        if current is None:
            self.add("lease", "ok", "no lease held")
            return
        rec, _ = current
        now = datetime.now(UTC)
        mine = rec.machine_id == self.state.machine_id
        age = int(rec.age(now).total_seconds())
        desc = f"held by {rec.holder_host} (pid {rec.pid}, `{rec.command}`), renewed {age}s ago"
        if rec.expired(now):
            self.add("lease", "ok", desc + "; expired, will be taken over on next write")
        elif mine:
            self.add("lease", "ok", desc + " (this machine)")
        else:
            self.add(
                "lease",
                "warn",
                desc + " (another machine)",
                "Writes will fail until it expires; use --force if that machine is gone.",
            )

    # --- models ------------------------------------------------------------------

    def check_llm(self, settings: Settings) -> None:
        from brain.core.llm import client as llm

        cfg = settings.llm
        if not cfg.configured:
            self.add("llm", "skip", "llm.base_url is not set", "Run `brain init` or set [llm] base_url.")
            return
        try:
            c = llm.openai_client(settings, transport=self.llm_transport)
        except (llm.LLMConfigError, TLSConfigError) as e:
            self.add("llm", "fail", str(e), f"export {cfg.api_key_env}=<key>" if "API key" in str(e) else None)
            return

        def attempt(name: str, fn: Callable[[], str], *, soft: bool = False) -> bool:
            try:
                self.add(name, "ok", fn())
                return True
            except Exception as e:  # noqa: BLE001
                self.add(name, "warn" if soft else "fail", llm.explain_error(e, cfg))
                return False

        def models() -> str:
            ids = llm.list_models(c)
            wanted = [m for m in (cfg.chat_model, cfg.extract_model, cfg.embed_model) if m]
            missing = [m for m in wanted if m not in ids]
            if missing:
                raise LookupError(f"gateway lists {len(ids)} models but not: {', '.join(missing)}")
            return f"gateway lists {len(ids)} models, including the configured ones"

        try:
            self.add("models", "ok", models())
        except LookupError as e:
            self.add("models", "warn", str(e), "Check the model names in [llm].")
        except Exception as e:  # noqa: BLE001
            msg = llm.explain_error(e, cfg)
            if isinstance(e, openai.APIConnectionError | openai.AuthenticationError):
                self.add("models", "fail", msg)
                return  # every other model check would fail the same way
            self.add(
                "models",
                "warn",
                f"cannot list models: {msg}",
                "Some gateways do not implement /models; the checks below still test each model directly.",
            )
        for role, model in (("chat", cfg.chat_model), ("extract", cfg.extract_model)):
            if not model:
                self.add(role, "skip", f"llm.{role}_model is not set")
                continue
            if role == "extract" and model == cfg.chat_model:
                self.add(role, "ok", f"{model} (same as chat model)")
                continue
            attempt(role, lambda m=model: f"{m} replied ({llm.chat_ping(c, m)!r})")
        if cfg.embed_model:

            def emb() -> str:
                r = llm.embed(c, cfg, ["brain doctor"])
                if cfg.embed_dim and r.dim != cfg.embed_dim:
                    raise ValueError(
                        f"{cfg.embed_model} returned {r.dim} dimensions but llm.embed_dim = {cfg.embed_dim}"
                    )
                return f"{cfg.embed_model} returns {r.dim} dimensions"

            attempt("embedding", emb)
        else:
            self.add("embedding", "skip", "llm.embed_model is not set")
        if any(r.name in ("chat", "extract") and r.status == "fail" for r in self.results):
            self.add("structured output", "skip", "skipped because a model call above failed")
        else:
            self.check_structured(settings)

    def check_structured(self, settings: Settings) -> None:
        """Which ways of requesting structured output the gateway accepts, and whether the configured one works."""
        from brain.core.llm.models import LLM, LLMError
        from brain.core.llm.schemas import Probe

        llm = LLM(settings, transport=self.llm_transport)
        works: dict[str, bool] = {}
        errors: dict[str, str] = {}
        for mode in ("tool", "native", "prompted"):
            try:
                works[mode] = llm.run("probe", "Run the connectivity test.", Probe, mode=mode).output.ok
            except LLMError as e:
                works[mode], errors[mode] = False, str(e)
        configured = settings.llm.structured_output
        effective = ("tool" if works["tool"] else "prompted") if configured == "auto" else configured
        summary = ", ".join(f"{m} {'works' if ok else 'fails'}" for m, ok in works.items())
        detail = f"{summary}; llm.structured_output = {configured!r} uses {effective}"
        if works[effective]:
            self.add("structured output", "ok", detail)
            return
        working = [m for m, ok in works.items() if ok]
        fix = (
            f"Set llm.structured_output = {working[0]!r}."
            if working
            else f"No mode works with {llm.model_name('extract')}: {errors.get(effective, '')}"
        )
        self.add("structured output", "fail", detail, fix)

    # --- index -------------------------------------------------------------------

    def check_index(self, settings: Settings, storage: Storage) -> None:
        from brain.core.services import index_path

        path = index_path(settings, storage)
        if not path.exists():
            self.add("index", "info", "no local index yet; it is built on the next command")
            return
        try:
            idx = index_db.IndexDB(path, embed_model=settings.llm.embed_model)
        except Exception as e:  # noqa: BLE001
            self.add("index", "fail", f"cannot open {path}: {e}", "Run `brain reindex`.")
            return
        try:
            if idx.rebuilt_reason:
                self.add("index", "info", f"index will be rebuilt: {idx.rebuilt_reason}")
            c = idx.conn
            n_entries = c.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            pending = c.execute("SELECT COUNT(*) FROM entries WHERE next_step != 'done'").fetchone()[0]
            review = c.execute("SELECT COUNT(*) FROM review_items").fetchone()[0]
            self.add("index", "ok", f"{n_entries} entries, {pending} not fully processed, {review} review items")
            failures = self.state.read_json("pipeline-failures.json")
            if failures:
                eid, f = sorted(failures.items(), key=lambda kv: kv[1].get("at", ""))[-1]
                self.add(
                    "pipeline",
                    "warn",
                    f"{len(failures)} entries failed processing; latest {eid} at "
                    f"{f['step']}: {f['error'].splitlines()[0]}",
                    "Fix the cause, then run `brain process`.",
                )
            gen = read_generation(storage)
            local = index_db.get_meta(c, "generation")
            if gen and gen.token != local:
                self.add("index", "info", "store has changes the index has not seen; the next command syncs them")
            problems = c.execute("SELECT key, message FROM problems ORDER BY key LIMIT 5").fetchall()
            total = c.execute("SELECT COUNT(*) FROM problems").fetchone()[0]
            for key, message in problems:
                self.add(
                    "file", "warn", f"{key}: {message.splitlines()[0]}", "Fix the file by hand; the index retries it."
                )
            if total > len(problems):
                self.add("file", "warn", f"... and {total - len(problems)} more files with problems")
        finally:
            idx.close()


def _filesystem_type(path: Path, mounts: str | None = None) -> str | None:
    """Filesystem type on Linux, from /proc/mounts. None elsewhere."""
    if mounts is None:
        proc = Path("/proc/mounts")
        if not proc.exists():
            return None
        mounts = proc.read_text()
    target = os.path.realpath(path)
    best, best_type = "", None
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mount_point = parts[1].replace("\\040", " ")
        if (target == mount_point or target.startswith(mount_point.rstrip("/") + "/")) and len(mount_point) > len(best):
            best, best_type = mount_point, parts[2]
    return best_type


def run_doctor(config: str | None = None, **kwargs) -> list[CheckResult]:
    return Doctor(config, **kwargs).run()
