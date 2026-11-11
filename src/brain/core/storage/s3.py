"""S3 and S3-compatible storage via boto3."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from boto3.session import Session
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from brain.core.config import StorageConfig, TLSConfig
from brain.core.storage.base import NotFound, ObjectInfo, PreconditionFailed, StorageError, check_key, valid_key
from brain.core.tls import boto_tls

_PRECONDITION_CODES = {"PreconditionFailed", "ConditionalRequestConflict", "412", "409"}
_NOT_FOUND_CODES = {"NoSuchKey", "404", "NotFound"}


class S3Storage:
    supports_conditional_writes: bool | None = None

    def __init__(self, cfg: StorageConfig, tls: TLSConfig, *, client: Any = None) -> None:
        self.bucket = cfg.bucket
        self.prefix = cfg.prefix
        self.endpoint_url = cfg.endpoint_url or None
        if client is None:
            try:
                client = _make_client(cfg, tls)
            except BotoCoreError as e:
                raise StorageError(f"cannot create the S3 client: {e}") from e
        self._client = client

    @property
    def ident(self) -> str:
        return f"s3:{self.endpoint_url or 'aws'}/{self.bucket}/{self.prefix}"

    def describe(self) -> str:
        where = f" at {self.endpoint_url}" if self.endpoint_url else ""
        return f"s3://{self.bucket}/{self.prefix}{where}"

    def _full(self, key: str) -> str:
        return self.prefix + check_key(key)

    def get(self, key: str) -> bytes:
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=self._full(key))
            return resp["Body"].read()
        except ClientError as e:
            if _code(e) in _NOT_FOUND_CODES:
                raise NotFound(key) from None
            raise _wrap(e, f"get {key}") from e
        except (BotoCoreError, NoCredentialsError) as e:
            raise _wrap(e, f"get {key}") from e

    def put(
        self,
        key: str,
        data: bytes,
        *,
        if_absent: bool = False,
        if_match: str | None = None,
    ) -> ObjectInfo:
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": self._full(key), "Body": data}
        if if_absent:
            kwargs["IfNoneMatch"] = "*"
        if if_match is not None:
            kwargs["IfMatch"] = _quote(if_match)
        try:
            resp = self._client.put_object(**kwargs)
        except ClientError as e:
            if _code(e) in _PRECONDITION_CODES:
                raise PreconditionFailed(f"{key}: {_code(e)}") from None
            raise _wrap(e, f"put {key}") from e
        except (BotoCoreError, NoCredentialsError) as e:
            raise _wrap(e, f"put {key}") from e
        return ObjectInfo(key=key, etag=_unquote(resp.get("ETag", "")), size=len(data), mtime=datetime.now(UTC))

    def list(self, prefix: str = "") -> Iterator[ObjectInfo]:
        full_prefix = self.prefix + prefix
        try:
            pages = self._client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=full_prefix)
            for page in pages:
                for obj in page.get("Contents", []):
                    key = obj["Key"][len(self.prefix) :]
                    if not key or key.endswith("/") or not valid_key(key):
                        continue  # a folder marker, or a name brain does not use (a tool's dot directory)
                    yield ObjectInfo(
                        key=key,
                        etag=_unquote(obj.get("ETag", "")),
                        size=obj.get("Size", 0),
                        mtime=obj.get("LastModified", datetime.now(UTC)),
                    )
        except (ClientError, BotoCoreError, NoCredentialsError) as e:
            raise _wrap(e, f"list {prefix or '(all)'}") from e

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=self._full(key))
        except (ClientError, BotoCoreError, NoCredentialsError) as e:
            raise _wrap(e, f"delete {key}") from e

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._full(key))
            return True
        except ClientError as e:
            if _code(e) in _NOT_FOUND_CODES:
                return False
            raise _wrap(e, f"head {key}") from e
        except (BotoCoreError, NoCredentialsError) as e:
            raise _wrap(e, f"head {key}") from e


def _make_client(cfg: StorageConfig, tls: TLSConfig) -> Any:
    bt = boto_tls(tls)
    config_kwargs: dict[str, Any] = {
        "retries": {"mode": "standard", "max_attempts": 5},
        "s3": {"addressing_style": cfg.addressing_style},
        # Since botocore 1.36, PUTs are sent aws-chunked with a trailing CRC32
        # checksum by default; several S3-compatible stores reject that. Set it
        # unconditionally, not only when cfg.endpoint_url is set, because the
        # endpoint can also come from AWS_ENDPOINT_URL(_S3) or ~/.aws/config.
        "request_checksum_calculation": "when_required",
        "response_checksum_validation": "when_required",
    }
    if bt.client_cert:
        config_kwargs["client_cert"] = bt.client_cert
    session = Session(profile_name=cfg.profile or None)
    return session.client(
        "s3",
        endpoint_url=cfg.endpoint_url or None,
        region_name=cfg.region or None,
        verify=bt.verify,
        config=Config(**config_kwargs),
    )


def _code(e: ClientError) -> str:
    err = e.response.get("Error", {})
    code = str(err.get("Code", ""))
    if not code:
        code = str(e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", ""))
    return code


def _wrap(e: Exception, op: str) -> StorageError:
    if isinstance(e, NoCredentialsError):
        return StorageError(
            f"S3 {op}: no AWS credentials found. Set AWS_PROFILE (or storage.profile), "
            "or AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY."
        )
    if isinstance(e, ClientError):
        code = _code(e)
        msg = e.response.get("Error", {}).get("Message", "")
        hint = ""
        if code in ("AccessDenied", "403"):
            hint = " Check that the credentials allow s3:GetObject/PutObject/DeleteObject/ListBucket on this bucket and prefix."
        elif code == "NoSuchBucket":
            hint = " Check storage.bucket and storage.endpoint_url."
        elif code in ("SignatureDoesNotMatch", "InvalidAccessKeyId"):
            hint = " Check the AWS credentials and storage.region."
        return StorageError(f"S3 {op} failed: {code} {msg}.{hint}".strip())
    text = str(e)
    hint = ""
    if "SSL" in text or "CERTIFICATE" in text.upper():
        hint = " Set [tls] ca_bundle to the CA that signed the S3 endpoint's certificate."
    elif "Could not connect" in text or "Connect timeout" in text:
        hint = " Check storage.endpoint_url and any HTTPS_PROXY / NO_PROXY settings."
    return StorageError(f"S3 {op} failed: {text}.{hint}")


def _unquote(etag: str) -> str:
    return etag.strip('"')


def _quote(etag: str) -> str:
    return etag if etag.startswith('"') else f'"{etag}"'
