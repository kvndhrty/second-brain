from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest

from brain.core.config import LoadedSettings, Settings, StorageConfig, TLSConfig
from brain.core.localstate import LocalState
from brain.core.services import Brain
from brain.core.storage.local import LocalStorage
from brain.core.storage.s3 import S3Storage

# MinIO no longer publishes public images (quay.io and Docker Hub both refuse
# anonymous pulls) or binaries, so this uses Chainguard's free build, pinned by
# digest (MinIO RELEASE.2026-10-23T20-06-51Z). Override with
# BRAIN_TEST_MINIO_IMAGE if this digest stops being available.
MINIO_IMAGE = os.environ.get(
    "BRAIN_TEST_MINIO_IMAGE",
    "cgr.dev/chainguard/minio@sha256:aea6c163749e1caca37f907976dcf337177ddf5f558808f2aa591618fd3a6eef",
)
MINIO_USER = "brainminio"
MINIO_PASSWORD = "brainminio-secret"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    for var in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
        d = tmp_path / "xdg" / var.lower()
        d.mkdir(parents=True)
        monkeypatch.setenv(var, str(d))
    # Config overrides from the developer's shell must not leak into tests.
    sections = set(Settings.model_fields)
    for name in list(os.environ):
        if name == "BRAIN_CONFIG" or (
            name.startswith("BRAIN_") and name[6:].split("__", 1)[0].lower() in sections and "__" in name
        ):
            monkeypatch.delenv(name)
    if request.node.get_closest_marker("live"):
        return  # live tests use the real AWS environment
    # Never read the developer's real AWS config or credentials.
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "aws-credentials"))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def state(tmp_path: Path) -> LocalState:
    return LocalState(tmp_path / "state")


@pytest.fixture
def local_storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "store")


@pytest.fixture
def moto_storage(monkeypatch: pytest.MonkeyPatch) -> Iterator[S3Storage]:
    from moto import mock_aws

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="brain-test")
        yield S3Storage(StorageConfig(backend="s3", bucket="brain-test", prefix="brain/"), TLSConfig())


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def minio_endpoint() -> Iterator[dict[str, str]]:
    """A real S3-compatible server: BRAIN_TEST_S3_ENDPOINT, or MinIO started with podman/docker."""
    unavailable = pytest.fail if os.environ.get("BRAIN_TEST_REQUIRE_S3") == "1" else pytest.skip
    if os.environ.get("BRAIN_TEST_S3_ENDPOINT"):
        yield {
            "endpoint": os.environ["BRAIN_TEST_S3_ENDPOINT"],
            "key": os.environ.get("BRAIN_TEST_S3_ACCESS_KEY", MINIO_USER),
            "secret": os.environ.get("BRAIN_TEST_S3_SECRET_KEY", MINIO_PASSWORD),
        }
        return
    runtime = next((r for r in ("podman", "docker") if shutil.which(r)), None)
    if runtime is None:
        unavailable("no BRAIN_TEST_S3_ENDPOINT and neither podman nor docker is installed")
    port = _free_port()
    name = f"brain-test-minio-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        [
            runtime,
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:9000",
            "-e",
            f"MINIO_ROOT_USER={MINIO_USER}",
            "-e",
            f"MINIO_ROOT_PASSWORD={MINIO_PASSWORD}",
            MINIO_IMAGE,
            "server",
            "/data",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        unavailable(f"could not start MinIO with {runtime}: {(run.stderr.strip().splitlines() or [''])[0][:200]}")
    endpoint = f"http://127.0.0.1:{port}"
    try:
        import httpx2

        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if httpx2.get(f"{endpoint}/minio/health/ready", timeout=1).status_code == 200:
                    break
            except httpx2.HTTPError:
                pass
            time.sleep(0.5)
        else:
            unavailable("MinIO did not become ready within 60s")
        yield {"endpoint": endpoint, "key": MINIO_USER, "secret": MINIO_PASSWORD}
    finally:
        subprocess.run([runtime, "rm", "-f", name], capture_output=True, check=False)


@pytest.fixture
def minio_storage(minio_endpoint: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> S3Storage:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", minio_endpoint["key"])
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", minio_endpoint["secret"])
    bucket = f"brain-test-{uuid.uuid4().hex[:8]}"
    boto3.client("s3", endpoint_url=minio_endpoint["endpoint"], region_name="us-east-1").create_bucket(Bucket=bucket)
    cfg = StorageConfig(
        backend="s3",
        bucket=bucket,
        prefix="brain/",
        endpoint_url=minio_endpoint["endpoint"],
        region="us-east-1",
        addressing_style="path",
    )
    return S3Storage(cfg, TLSConfig())


@pytest.fixture(params=["local", "moto", "minio"])
def storage(request: pytest.FixtureRequest):
    return request.getfixturevalue(f"{request.param}_storage")


def make_brain(
    storage,
    state: LocalState,
    tmp_path: Path,
    *,
    index_name: str | None = None,
    fake_llm: bool = False,
    responders: dict | None = None,
    **settings_kw,
) -> Brain:
    settings = Settings(**settings_kw)
    settings.index.path = str(tmp_path / "index" / f"{index_name or uuid.uuid4().hex[:6]}.db")
    loaded = LoadedSettings(settings=settings, path=None, env_overrides=[])
    llm = None
    if fake_llm:
        from brain.core.llm.fake import FakeLLM

        llm = FakeLLM(settings, responders=responders)
    return Brain(loaded, command="pytest", storage=storage, state=state, llm=llm)


FAKE_LLM_SETTINGS = {"base_url": "https://fake/v1", "chat_model": "fake-chat", "embed_model": "fake-embed"}


@pytest.fixture
def brain(local_storage, state, tmp_path):
    """A Brain on local storage with the scripted fake LLM."""
    b = make_brain(local_storage, state, tmp_path, fake_llm=True, llm=FAKE_LLM_SETTINGS)
    yield b
    b.close()
