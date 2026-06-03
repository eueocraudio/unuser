"""Armazenamento cofre-cego (zero-knowledge).

O servidor guarda apenas dados **já cifrados pelo cliente** e nunca os decifra:

* **blobs** — blocos de conteúdo, endereçados pelo ``block_id`` opaco;
* **manifesto** — blob cifrado + ``vault_version`` em claro (para o CAS).

Toda entrada vinda da rede é validada com rigor (formato do ``block_id``) para
evitar travessia de caminho — o servidor nunca confia no que recebe.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

# block_id = "b:" + 64 hex (HMAC-SHA256). Validação estrita anti path-traversal.
_BLOCK_RE = re.compile(r"^b:[0-9a-f]{64}$")


class StorageError(Exception):
    """Erro genérico do armazenamento."""


class InvalidIdError(StorageError):
    """block_id com formato inválido."""


class NotFoundError(StorageError):
    """Recurso inexistente."""


class ConflictError(StorageError):
    """CAS falhou: a versão esperada não bate com a atual."""

    def __init__(self, current: int, expected: int):
        super().__init__(f"conflito de versão: atual={current}, esperada={expected}")
        self.current = current
        self.expected = expected


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class BlindStorage:
    def __init__(self, root):
        self.root = Path(root)
        self.blobs_dir = self.root / "blobs"
        self.manifest_dir = self.root / "manifest"
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        self._manifest_lock = threading.Lock()

    # --- blobs --------------------------------------------------------------

    def _blob_path(self, block_id: str) -> Path:
        if not _BLOCK_RE.match(block_id):
            raise InvalidIdError(f"block_id inválido: {block_id!r}")
        return self.blobs_dir / (block_id[2:] + ".blob")  # remove "b:"

    def has_blob(self, block_id: str) -> bool:
        return self._blob_path(block_id).exists()

    def put_blob(self, block_id: str, data: bytes) -> None:
        """Idempotente: blobs são endereçados por conteúdo, então não re-grava."""
        path = self._blob_path(block_id)
        if path.exists():
            return
        _atomic_write(path, data)

    def get_blob(self, block_id: str) -> bytes:
        path = self._blob_path(block_id)
        if not path.exists():
            raise NotFoundError(f"blob inexistente: {block_id}")
        return path.read_bytes()

    def delete_blob(self, block_id: str) -> None:
        self._blob_path(block_id).unlink(missing_ok=True)

    def list_blobs(self) -> list[str]:
        return sorted("b:" + p.stem for p in self.blobs_dir.glob("*.blob"))

    # --- manifesto (com CAS) ------------------------------------------------

    def _current_path(self) -> Path:
        return self.manifest_dir / "current.json"

    def manifest_version(self) -> int:
        cur = self._current_path()
        if not cur.exists():
            return 0
        return json.loads(cur.read_bytes())["version"]

    def get_manifest(self) -> tuple[int, bytes] | None:
        """(version, blob) do manifesto atual, ou None se ainda não há nenhum."""
        cur = self._current_path()
        if not cur.exists():
            return None
        meta = json.loads(cur.read_bytes())
        blob = (self.manifest_dir / meta["blob"]).read_bytes()
        return meta["version"], blob

    def put_manifest(self, expected_version: int, new_version: int, blob: bytes) -> None:
        """Compare-and-swap: só grava se a versão atual == ``expected_version``.

        Levanta :class:`ConflictError` se outro cliente atualizou nesse meio-tempo.
        """
        with self._manifest_lock:
            current = self.manifest_version()
            if current != expected_version:
                raise ConflictError(current, expected_version)
            if new_version <= current:
                raise StorageError(
                    f"new_version ({new_version}) deve ser maior que a atual ({current})"
                )
            name = f"v{new_version:012d}.bin"
            _atomic_write(self.manifest_dir / name, blob)
            _atomic_write(
                self._current_path(),
                json.dumps({"version": new_version, "blob": name}).encode("utf-8"),
            )
