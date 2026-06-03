"""Motor de sincronização do cliente — diff de 3 vias e status por arquivo (seção 7.1).

Compara três estados, por caminho de cofre (*vault path*):

* **local**  — ``content_hash`` do arquivo em disco agora (via :mod:`scanner`);
* **base**   — o hash da última sincronização, guardado no índice local (:mod:`index`);
* **servidor** — o hash da versão atual no manifesto cifrado (:mod:`manifest`).

A base é o "ancestral comum": é ela que distingue *quem* mudou desde o último acordo.
Este módulo só **classifica** (leitura pura); as ações que mutam (enviar/baixar) virão
em cima desta visão.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from . import scanner
from .index import Index
from .manifest import VaultManifest

# Sentinela para "o servidor tem um tombstone" (arquivo apagado, mas com histórico).
DELETED = "<deleted>"


class FileStatus(str, Enum):
    """Os seis status da seção 7.1 (o valor é o rótulo exibível)."""

    IN_SYNC = "Em sincronia"
    LOCAL_MODIFIED = "Modificado no local"
    SERVER_MODIFIED = "Modificado no servidor"
    CONFLICT = "Conflito"
    LOCAL_ONLY = "Só local"
    SERVER_ONLY = "Só no servidor"


@dataclass(frozen=True)
class FileState:
    vault_path: str
    status: FileStatus
    local_hash: str | None
    base_hash: str | None
    remote_hash: str | None   # hash, None (ausente) ou DELETED (tombstone)


def classify(local: str | None, base: str | None, remote: str | None) -> FileStatus:
    """Decide o status a partir dos três hashes (qualquer um pode ser ``None``).

    ``remote`` pode ser :data:`DELETED` (tombstone). É função pura — toda a regra de
    negócio do status vive aqui, isolada de disco e de rede.
    """
    # Tombstone do servidor conta como "servidor não tem mais o conteúdo".
    remote_gone = remote is None or remote == DELETED

    # Nenhum dos lados tem o arquivo de fato → nada a mostrar como divergência.
    if local is None and remote_gone:
        return FileStatus.IN_SYNC          # ex.: apagado nos dois lados

    # Nunca houve acordo anterior (sem base): é exclusivo de um dos lados.
    if base is None:
        if local is not None and remote_gone:
            return FileStatus.LOCAL_ONLY
        if local is None and not remote_gone:
            return FileStatus.SERVER_ONLY
        # presente nos dois sem base comum: se idênticos, sincronia; senão, conflito.
        return FileStatus.IN_SYNC if local == remote else FileStatus.CONFLICT

    # Há base. Quem divergiu dela?
    changed_local = local != base
    changed_remote = remote_gone or remote != base

    if not changed_local and not changed_remote:
        return FileStatus.IN_SYNC
    if changed_local and changed_remote:
        # ambos mudaram; se por acaso convergiram para o mesmo conteúdo, é sincronia.
        if local is not None and not remote_gone and local == remote:
            return FileStatus.IN_SYNC
        return FileStatus.CONFLICT
    return FileStatus.LOCAL_MODIFIED if changed_local else FileStatus.SERVER_MODIFIED


def _remote_hashes(manifest: VaultManifest) -> dict[str, str]:
    """``vault_path -> hash`` (ou :data:`DELETED`) da versão atual de cada arquivo."""
    out: dict[str, str] = {}
    for fm in manifest.files.values():
        cur = fm.current
        out[fm.path] = DELETED if cur.deleted else (cur.hash or DELETED)
    return out


class SyncEngine:
    """Junta scanner local, índice (base) e manifesto (servidor) numa visão de status."""

    def __init__(self, index: Index):
        self.index = index

    def base_hashes(self) -> dict[str, str]:
        """``vault_path -> hash`` da última sincronização, lido do índice local."""
        return {rec.path: rec.hash for rec in self.index.all_files()}

    def status(self, scanned: dict[str, scanner.ScannedFile],
               manifest: VaultManifest | None) -> list[FileState]:
        """Status de cada arquivo na união (local ∪ base ∪ servidor), ordenado por path."""
        local = {vp: sf.hash for vp, sf in scanned.items()}
        base = self.base_hashes()
        remote = _remote_hashes(manifest) if manifest is not None else {}

        out: list[FileState] = []
        for vp in sorted(set(local) | set(base) | set(remote)):
            lh, bh, rh = local.get(vp), base.get(vp), remote.get(vp)
            out.append(FileState(vp, classify(lh, bh, rh), lh, bh, rh))
        return out
