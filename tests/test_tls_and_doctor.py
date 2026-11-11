"""TLS/mTLS against a real local HTTPS server, and `brain doctor` against the fake model endpoint."""

from __future__ import annotations

import ssl
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx2
import pytest
import trustme

from brain.core.config import LoadedSettings, Settings, StorageConfig, TLSConfig
from brain.core.doctor import Doctor, _filesystem_type
from brain.core.llm.fake import FakeOpenAI
from brain.core.storage.s3 import _make_client
from brain.core.tls import TLSConfigError, ssl_context


class Certs:
    def __init__(self, root: Path) -> None:
        self.ca = trustme.CA()
        self.server = self.ca.issue_cert("127.0.0.1", "localhost")
        client = self.ca.issue_cert("brain-client@example.com")
        self.ca_file = root / "ca.pem"
        self.client_cert = root / "client.pem"
        self.client_key = root / "client.key"
        self.ca.cert_pem.write_to_path(str(self.ca_file))
        client.cert_chain_pems[0].write_to_path(str(self.client_cert))
        client.private_key_pem.write_to_path(str(self.client_key))


@pytest.fixture
def certs(tmp_path: Path) -> Certs:
    return Certs(tmp_path)


@pytest.fixture
def mtls_fake_gateway(certs: Certs) -> Iterator[tuple[str, FakeOpenAI]]:
    """An HTTPS server that requires a client certificate and answers like an OpenAI-compatible gateway."""
    fake = FakeOpenAI()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else b""
            req = httpx2.Request(
                self.command, f"https://127.0.0.1{self.path}", headers=dict(self.headers), content=body
            )
            resp = fake._handle(req)
            self.send_response(resp.status_code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(resp.content)))
            self.end_headers()
            self.wfile.write(resp.content)

        do_GET = do_POST = _serve

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    certs.server.configure_cert(ctx)
    certs.ca.configure_trust(ctx)
    ctx.verify_mode = ssl.CERT_REQUIRED
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"https://127.0.0.1:{server.server_address[1]}/v1", fake
    finally:
        server.shutdown()


def test_untrusted_ca_fails_with_os_trust_store(mtls_fake_gateway):
    url, _ = mtls_fake_gateway
    with httpx2.Client(verify=ssl_context(TLSConfig())) as c, pytest.raises(httpx2.ConnectError):
        c.get(f"{url}/models")


def test_bad_tls_paths_raise_clear_errors(tmp_path, certs):
    with pytest.raises(TLSConfigError, match="not found"):
        ssl_context(TLSConfig(ca_bundle=str(tmp_path / "missing.pem")))
    with pytest.raises(TLSConfigError):
        ssl_context(TLSConfig(ca_bundle=str(certs.ca_file), client_cert=str(certs.ca_file)))  # no key


def test_boto_client_gets_ca_and_client_cert(certs):
    cfg = StorageConfig(backend="s3", bucket="b", endpoint_url="https://s3.internal")
    client = _make_client(
        cfg,
        TLSConfig(ca_bundle=str(certs.ca_file), client_cert=str(certs.client_cert), client_key=str(certs.client_key)),
    )
    assert client.meta.config.client_cert == (str(certs.client_cert), str(certs.client_key))
    assert client.meta.config.request_checksum_calculation == "when_required"
    assert client._endpoint.http_session._verify == str(certs.ca_file)


@pytest.mark.parametrize("from_env", [False, True], ids=["config", "AWS_ENDPOINT_URL_S3"])
def test_boto_put_is_not_sent_with_streaming_checksum(monkeypatch, from_env):
    # Some S3-compatible stores reject aws-chunked bodies with a trailing checksum.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "y")
    if from_env:
        monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "https://s3.internal")
    cfg = StorageConfig(
        backend="s3", bucket="b", region="us-east-1", endpoint_url="" if from_env else "https://s3.internal"
    )
    client = _make_client(cfg, TLSConfig())
    sent = {}

    def capture(request, **_):
        sent.update({k.lower(): v for k, v in request.headers.items()})
        raise RuntimeError("captured")

    client.meta.events.register("before-send.s3", capture)
    with pytest.raises(RuntimeError, match="captured"):
        client.put_object(Bucket="b", Key="k", Body=b"hello", IfNoneMatch="*")
    assert "content-encoding" not in sent
    assert "x-amz-trailer" not in sent
    assert not any(k.startswith("x-amz-checksum") for k in sent)


# --- doctor ------------------------------------------------------------------


def doctor(local_storage, state, transport=None, **settings_kw) -> dict[str, list]:
    settings = Settings(**settings_kw)
    loaded = LoadedSettings(settings=settings, path=None, env_overrides=[])
    results = Doctor(storage=local_storage, state=state, llm_transport=transport, loaded=loaded).run()
    by_name: dict[str, list] = {}
    for r in results:
        by_name.setdefault(r.name, []).append(r)
    return by_name


LLM = {
    "base_url": "https://gateway.test/v1",
    "small_model": "small-model",
    "strong_model": "strong-model",
    "embed_model": "embed-model",
}


def test_doctor_all_green_with_fake_gateway(local_storage, state, monkeypatch):
    monkeypatch.setenv("BRAIN_API_KEY", "test-key")
    fake = FakeOpenAI(dim=12)
    r = doctor(local_storage, state, fake.transport(), llm=LLM)
    for name in (
        "storage",
        "conditional writes",
        "lease",
        "models",
        "small model",
        "strong model",
        "embedding",
        "sqlite-vec",
    ):
        assert r[name][0].status == "ok", (name, r[name][0])
    assert "12 dimensions" in r["embedding"][0].detail
    assert not list(local_storage.list("_system/doctor/"))


def test_doctor_reports_missing_key(local_storage, state, monkeypatch):
    monkeypatch.delenv("BRAIN_API_KEY", raising=False)
    r = doctor(local_storage, state, FakeOpenAI().transport(), llm=LLM)
    assert r["llm"][0].status == "fail"
    assert "BRAIN_API_KEY" in r["llm"][0].detail


def test_doctor_reports_bad_key_once(local_storage, state, monkeypatch):
    monkeypatch.setenv("BRAIN_API_KEY", "wrong")
    r = doctor(local_storage, state, FakeOpenAI().transport(), llm=LLM)
    assert r["models"][0].status == "fail" and "401" in r["models"][0].detail
    assert "small model" not in r


def test_doctor_reports_wrong_model_and_dim(local_storage, state, monkeypatch):
    monkeypatch.setenv("BRAIN_API_KEY", "test-key")
    r = doctor(
        local_storage, state, FakeOpenAI(dim=12).transport(), llm=LLM | {"small_model": "typo-model", "embed_dim": 1024}
    )
    assert r["models"][0].status == "warn" and "typo-model" in r["models"][0].detail
    assert r["small model"][0].status == "fail" and "404" in r["small model"][0].detail
    assert r["embedding"][0].status == "fail" and "1024" in r["embedding"][0].detail


def test_doctor_over_real_mtls(local_storage, state, certs, mtls_fake_gateway, monkeypatch):
    url, fake = mtls_fake_gateway
    monkeypatch.setenv("BRAIN_API_KEY", "test-key")
    tls = {"ca_bundle": str(certs.ca_file), "client_cert": str(certs.client_cert), "client_key": str(certs.client_key)}
    r = doctor(local_storage, state, llm=LLM | {"base_url": url}, tls=tls)
    assert r["small model"][0].status == "ok", r["small model"][0]
    assert r["embedding"][0].status == "ok"
    r = doctor(local_storage, state, llm=LLM | {"base_url": url}, tls={"ca_bundle": str(certs.ca_file)})
    assert r["models"][0].status == "fail"


def test_doctor_warns_when_localhost_goes_through_proxy(local_storage, state, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    r = doctor(local_storage, state, llm={"base_url": "https://localhost:8443/v1"})
    assert r["proxy"][0].status == "warn" and "NO_PROXY" in r["proxy"][0].fix
    monkeypatch.setenv("NO_PROXY", "localhost")
    r = doctor(local_storage, state, llm={"base_url": "https://localhost:8443/v1"})
    assert r["proxy"][0].status == "ok"


def test_filesystem_type_from_mounts(tmp_path):
    mounts = f"server:/home {tmp_path} nfs4 rw 0 0\n/dev/sda1 / ext4 rw 0 0\n"
    assert _filesystem_type(tmp_path / "sub", mounts) == "nfs4"
    assert _filesystem_type(Path("/usr"), mounts) == "ext4"
