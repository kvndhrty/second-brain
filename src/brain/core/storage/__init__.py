from brain.core.config import Settings
from brain.core.storage.base import NotFound, ObjectInfo, PreconditionFailed, Storage, StorageError

__all__ = ["NotFound", "ObjectInfo", "PreconditionFailed", "Storage", "StorageError", "open_storage"]


def open_storage(settings: Settings) -> Storage:
    cfg = settings.storage
    if cfg.backend == "s3":
        from brain.core.storage.s3 import S3Storage

        return S3Storage(cfg, settings.tls)
    from brain.core.storage.local import LocalStorage

    return LocalStorage(cfg.local_root())
