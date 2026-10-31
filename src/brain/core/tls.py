"""One TLS configuration for both the model endpoint (httpx2) and S3 (boto3).

Trust comes from `tls.ca_bundle` if set, otherwise from the OS trust store via
`truststore`, so a corporate CA installed system-wide works with no config.
boto3 cannot take an SSLContext, so it gets a CA file path instead.
"""

from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from pathlib import Path

import truststore

from brain.core.config import TLSConfig

# Distribution CA bundles, in the order truststore and most tools check them.
SYSTEM_CA_FILES = (
    "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL, Fedora
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, Alpine
    "/etc/ssl/ca-bundle.pem",  # SUSE
    "/etc/pki/tls/cacert.pem",
    "/etc/ssl/cert.pem",  # macOS (does not include keychain-only certs)
)


class TLSConfigError(Exception):
    pass


def _expand(p: str) -> str:
    return str(Path(p).expanduser())


def ssl_context(cfg: TLSConfig) -> ssl.SSLContext:
    try:
        if cfg.ca_bundle:
            ctx = ssl.create_default_context(cafile=_expand(cfg.ca_bundle))
        else:
            ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if cfg.client_cert:
            ctx.load_cert_chain(_expand(cfg.client_cert), _expand(cfg.client_key) if cfg.client_key else None)
    except FileNotFoundError as e:
        raise TLSConfigError(f"TLS file not found: {e.filename}") from e
    except ssl.SSLError as e:
        raise TLSConfigError(f"cannot load TLS files from [tls] config: {e}") from e
    return ctx


def system_ca_file() -> str | None:
    for p in SYSTEM_CA_FILES:
        if os.path.isfile(p):
            return p
    return None


@dataclass(frozen=True)
class BotoTLS:
    verify: str | None  # None lets botocore use AWS_CA_BUNDLE or its default
    client_cert: tuple[str, str] | str | None
    source: str


def boto_tls(cfg: TLSConfig) -> BotoTLS:
    if cfg.client_cert:
        cert: tuple[str, str] | str | None = (
            (_expand(cfg.client_cert), _expand(cfg.client_key)) if cfg.client_key else _expand(cfg.client_cert)
        )
    else:
        cert = None
    if cfg.ca_bundle:
        return BotoTLS(_expand(cfg.ca_bundle), cert, f"tls.ca_bundle ({cfg.ca_bundle})")
    if os.environ.get("AWS_CA_BUNDLE"):
        return BotoTLS(None, cert, f"AWS_CA_BUNDLE ({os.environ['AWS_CA_BUNDLE']})")
    sys_file = system_ca_file()
    if sys_file and sys_file != "/etc/ssl/cert.pem":
        return BotoTLS(sys_file, cert, f"system CA file ({sys_file})")
    return BotoTLS(None, cert, "botocore default CA bundle")


def describe_trust(cfg: TLSConfig) -> str:
    if cfg.ca_bundle:
        return f"tls.ca_bundle ({cfg.ca_bundle})"
    return "OS trust store (truststore)"
