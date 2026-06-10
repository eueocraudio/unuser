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
# Nome de backup gerado por nós: unuser-backup-YYYYMMDD-HHMMSS.tar (validado anti-traversal).
_BACKUP_RE = re.compile(r"^unuser-backup-[0-9]{8}-[0-9]{6}\.tar$")


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
    # Sufixo único por escrita: sob ThreadingHTTPServer, dois PUTs do MESMO block_id
    # (dedup por conteúdo) escreveriam no mesmo .tmp e corromperiam um ao outro. Cada
    # escritor publica seu próprio inode via os.replace; o "perdedor" é idempotente.
    tmp = path.with_name(f"{path.name}.{os.urandom(8).hex()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _safe_extract(tar, dest: Path) -> None:
    """Extrai recusando membros que escapem de ``dest`` **ou** que não sejam arquivo/
    diretório comum (defesa em profundidade — o tar é gerado por nós, mas nunca confiamos
    no que entra). Rejeitar symlink/hardlink/dispositivo barra o ataque clássico de
    extração via symlink mesmo no Python 3.11, que não tem o ``filter='data'`` abaixo."""
    dest = dest.resolve()
    base = str(dest) + os.sep
    for member in tar.getmembers():
        if not (member.isfile() or member.isdir()):
            raise StorageError(f"membro de tar não-regular: {member.name!r}")
        target = (dest / member.name).resolve()
        if target != dest and not str(target).startswith(base):
            raise StorageError(f"caminho de tar inseguro: {member.name!r}")
    try:
        tar.extractall(dest, filter="data")              # endurece (Py ≥ 3.12); valida metadados
    except TypeError:                                    # 3.11 sem o parâmetro 'filter'
        tar.extractall(dest)


class BlindStorage:
    def __init__(self, root, backups_dir=None):
        self.root = Path(root)
        self.blobs_dir = self.root / "blobs"
        self.manifest_dir = self.root / "manifest"
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        # Backups num diretório à parte — idealmente em mídia secundária (UNUSERD_BACKUPS).
        # NUNCA dentro de blobs/manifest: o export empacota só esses dois, então backups
        # antigos não entram em backups novos.
        self.backups_dir = Path(backups_dir) if backups_dir else (self.root / "backups")
        self.backups_dir.mkdir(parents=True, exist_ok=True)
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

    # --- uso de disco -------------------------------------------------------

    def _vault_size(self) -> tuple[int, int]:
        """(bytes ocupados pelo cofre, nº de blobs). Soma blobs + arquivos do manifesto."""
        total = n = 0
        for p in self.blobs_dir.glob("*.blob"):
            try:
                total += p.stat().st_size
                n += 1
            except OSError:
                pass
        for p in self.manifest_dir.glob("*"):
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return total, n

    def disk_usage(self) -> dict:
        """Uso do disco FÍSICO onde mora o storage (não do cofre). É metadado de infra —
        não vaza conteúdo/nomes/chaves — e alimenta a barra de uso no cliente."""
        import shutil
        du = shutil.disk_usage(self.root)
        vault_bytes, blob_count = self._vault_size()
        return {"disk_total": du.total, "disk_used": du.used, "disk_free": du.free,
                "vault_bytes": vault_bytes, "blob_count": blob_count}

    # --- export / import (backup server-side) -------------------------------

    def export(self) -> dict:
        """Empacota ``blobs/`` + ``manifest/`` num ``.tar`` no ``backups_dir`` (mídia
        secundária). Server-side: o cliente só dispara. Devolve {name, size, created}."""
        import tarfile
        import time
        name = "unuser-backup-" + time.strftime("%Y%m%d-%H%M%S") + ".tar"
        dest = self.backups_dir / name
        tmp = dest.with_name(name + f".{os.urandom(6).hex()}.tmp")
        with tarfile.open(tmp, "w") as tar:
            tar.add(self.blobs_dir, arcname="blobs")
            tar.add(self.manifest_dir, arcname="manifest")
        os.replace(tmp, dest)
        st = dest.stat()
        return {"name": name, "size": st.st_size, "created": int(st.st_mtime)}

    def list_backups(self) -> list[dict]:
        """Backups disponíveis no ``backups_dir`` (mais recentes primeiro)."""
        out: list[dict] = []
        for p in self.backups_dir.glob("unuser-backup-*.tar"):
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({"name": p.name, "size": st.st_size, "created": int(st.st_mtime)})
        out.sort(key=lambda d: d["created"], reverse=True)
        return out

    @staticmethod
    def _swap_dir(target: Path, new: Path) -> None:
        """Substitui ``target`` por ``new`` minimizando a janela (rename, não cópia)."""
        import shutil
        trash = target.with_name(target.name + ".old")
        shutil.rmtree(trash, ignore_errors=True)
        if target.exists():
            os.replace(target, trash)
        os.replace(new, target)
        shutil.rmtree(trash, ignore_errors=True)

    def restore(self, name: str) -> int:
        """Restaura ``blobs/`` + ``manifest/`` de um backup. **DESTRUTIVO**: substitui o
        estado atual do cofre. Devolve a versão do manifesto após restaurar."""
        if not _BACKUP_RE.match(name):
            raise InvalidIdError(f"nome de backup inválido: {name!r}")
        src = self.backups_dir / name
        if not src.exists():
            raise NotFoundError(f"backup inexistente: {name}")
        import shutil
        import tarfile
        import tempfile
        with self._manifest_lock:
            staging = Path(tempfile.mkdtemp(dir=self.root, prefix=".restore-"))
            try:
                with tarfile.open(src, "r") as tar:
                    _safe_extract(tar, staging)
                new_blobs, new_manifest = staging / "blobs", staging / "manifest"
                if not new_blobs.is_dir() or not new_manifest.is_dir():
                    raise StorageError("backup sem blobs/ ou manifest/")
                self._swap_dir(self.blobs_dir, new_blobs)
                self._swap_dir(self.manifest_dir, new_manifest)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        return self.manifest_version()
