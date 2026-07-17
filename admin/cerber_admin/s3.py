"""Обёртка над boto3 для S3-совместимых хранилищ (Spaces, MinIO, AWS...).

boto3 синхронный — все вызовы уводятся в тредпул через anyio.
Клиент кэшируется по версии конфига: смена настроек в UI сразу
пересоздаёт клиент.

Presigned-ссылки подписываются отдельным клиентом с public_url,
если он задан: подпись SigV4 включает host, поэтому URL должен
строиться на том адресе, по которому пойдёт браузер.
"""

from __future__ import annotations

import functools
from typing import Any

import anyio.to_thread
import boto3
from botocore.config import Config as BotoConfig

from cerber_admin.config import S3Config


class S3Service:
    def __init__(self, cfg: S3Config):
        self.cfg = cfg
        self._client = self._make_client(cfg.endpoint_url or None)
        public = cfg.public_url or cfg.endpoint_url
        self._presign_client = (
            self._client if public == (cfg.endpoint_url or "") else self._make_client(public or None)
        )

    def _make_client(self, endpoint: str | None):
        return boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=self.cfg.region or None,
            aws_access_key_id=self.cfg.access_key,
            aws_secret_access_key=self.cfg.secret_key,
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path" if self.cfg.force_path_style else "auto"},
                retries={"max_attempts": 3},
            ),
        )

    async def _run(self, fn, /, *args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    # --- multipart-загрузка (чанки от агента транслируются в parts) ---

    async def create_multipart(self, key: str, content_type: str) -> str:
        resp = await self._run(
            self._client.create_multipart_upload,
            Bucket=self.cfg.bucket, Key=key, ContentType=content_type,
        )
        return resp["UploadId"]

    async def upload_part(self, key: str, upload_id: str, part_number: int, body: bytes) -> str:
        resp = await self._run(
            self._client.upload_part,
            Bucket=self.cfg.bucket, Key=key, UploadId=upload_id,
            PartNumber=part_number, Body=body,
        )
        return resp["ETag"]

    async def complete_multipart(self, key: str, upload_id: str, parts: list[dict[str, Any]]) -> None:
        await self._run(
            self._client.complete_multipart_upload,
            Bucket=self.cfg.bucket, Key=key, UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )

    async def abort_multipart(self, key: str, upload_id: str) -> None:
        try:
            await self._run(
                self._client.abort_multipart_upload,
                Bucket=self.cfg.bucket, Key=key, UploadId=upload_id,
            )
        except Exception:  # noqa: BLE001 — abort лучший-effort: загрузки чистит и lifecycle
            pass

    # --- одиночные объекты ---

    async def put_object(self, key: str, body: bytes, content_type: str) -> None:
        await self._run(
            self._client.put_object,
            Bucket=self.cfg.bucket, Key=key, Body=body, ContentType=content_type,
        )

    async def delete_objects(self, keys: list[str]) -> None:
        keys = [k for k in keys if k]
        if not keys:
            return
        for i in range(0, len(keys), 1000):
            chunk = keys[i : i + 1000]
            await self._run(
                self._client.delete_objects,
                Bucket=self.cfg.bucket,
                Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True},
            )

    async def presign_get(self, key: str) -> str:
        return await self._run(
            self._presign_client.generate_presigned_url,
            "get_object",
            Params={"Bucket": self.cfg.bucket, "Key": key},
            ExpiresIn=self.cfg.presign_ttl_s,
        )

    async def check(self) -> None:
        """Проверка доступа: head bucket + пробная запись и удаление."""
        await self._run(self._client.head_bucket, Bucket=self.cfg.bucket)
        probe_key = f"{self.cfg.prefix}/.cerber-check"
        await self.put_object(probe_key, b"ok", "text/plain")
        await self.delete_objects([probe_key])


_cache: tuple[int, S3Service] | None = None


def get_s3(cfg: S3Config, version: int) -> S3Service:
    global _cache
    if _cache is None or _cache[0] != version:
        _cache = (version, S3Service(cfg))
    return _cache[1]
