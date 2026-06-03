"""Testes do motor de sincronização: classificador de status e diff de 3 vias."""

import pytest

from client import crypto, sync
from client.index import FileRecord, Index
from client.manifest import VaultManifest
from client.scanner import ScannedFile
from client.sync import DELETED, FileStatus, SyncEngine, classify

A, B, C = "blake3:aa", "blake3:bb", "blake3:cc"   # hashes fictícios distintos


# --- classificador puro (tabela-verdade da seção 7.1) -----------------------

@pytest.mark.parametrize("local, base, remote, esperado", [
    # local, base, servidor                       -> status
    (A,    A,    A,    FileStatus.IN_SYNC),         # idênticos
    (A,    None, None, FileStatus.LOCAL_ONLY),      # novo, nunca enviado
    (None, None, A,    FileStatus.SERVER_ONLY),     # existe só no cofre
    (B,    A,    A,    FileStatus.LOCAL_MODIFIED),   # mudou só no local
    (A,    A,    B,    FileStatus.SERVER_MODIFIED),  # mudou só no servidor
    (B,    A,    C,    FileStatus.CONFLICT),         # mudou nos dois, diferente
    (B,    A,    B,    FileStatus.IN_SYNC),          # mudou nos dois, convergiu igual
    (A,    A,    A,    FileStatus.IN_SYNC),
])
def test_classify_tabela(local, base, remote, esperado):
    assert classify(local, base, remote) == esperado


@pytest.mark.parametrize("local, base, remote, esperado", [
    (None, A,    A,       FileStatus.LOCAL_MODIFIED),   # apagado no local, servidor igual
    (None, A,    B,       FileStatus.CONFLICT),          # apagado no local, servidor mudou
    (A,    A,    DELETED, FileStatus.SERVER_MODIFIED),   # servidor apagou, local intacto
    (B,    A,    DELETED, FileStatus.CONFLICT),          # servidor apagou, local mudou
    (None, A,    DELETED, FileStatus.IN_SYNC),           # apagado nos dois
])
def test_classify_delecoes(local, base, remote, esperado):
    assert classify(local, base, remote) == esperado


# --- diff de 3 vias integrando índice + manifesto ---------------------------

@pytest.fixture
def kr():
    return crypto.unlock("s", b"kf", crypto.generate_salt(),
                         time_cost=1, memory_cost=8 * 1024, parallelism=1)


def _scanned(vault_path, h) -> ScannedFile:
    return ScannedFile(vault_path, f"/abs/{vault_path}", 1, 0.0, "100644", h)


def _manifest_com(kr, path, h, *, deleted=False) -> VaultManifest:
    v = VaultManifest.new("v:x", crypto.generate_salt())
    fk = crypto.generate_file_key()
    v.record_version(path=path, fk=fk, kek=kr.kek, device="d",
                     content_hash=h, size=1, blocks=[])
    if deleted:
        v.mark_deleted(path=path, device="d")
    return v


def test_status_integra_local_base_servidor(tmp_path, kr):
    with Index(tmp_path / "i.db") as idx:
        # base: "Doc/a.txt" sincronizado no hash A
        idx.upsert_file(FileRecord("f:1", "Doc/a.txt", 1, 0.0, A))

        scanned = {"Doc/a.txt": _scanned("Doc/a.txt", B)}   # mudou local -> B
        manifest = _manifest_com(kr, "Doc/a.txt", A)        # servidor ainda em A

        eng = SyncEngine(idx)
        estados = {fs.vault_path: fs.status for fs in eng.status(scanned, manifest)}
        assert estados == {"Doc/a.txt": FileStatus.LOCAL_MODIFIED}


def test_status_cobre_uniao_de_todos_os_lados(tmp_path, kr):
    with Index(tmp_path / "i.db") as idx:
        idx.upsert_file(FileRecord("f:1", "comum.txt", 1, 0.0, A))  # base p/ "comum"

        scanned = {
            "comum.txt": _scanned("comum.txt", A),     # igual em todos -> IN_SYNC
            "novo.txt": _scanned("novo.txt", C),       # só local -> LOCAL_ONLY
        }
        manifest = VaultManifest.new("v:x", crypto.generate_salt())
        fk = crypto.generate_file_key()
        manifest.record_version(path="comum.txt", fk=fk, kek=kr.kek, device="d",
                                content_hash=A, size=1, blocks=[])
        manifest.record_version(path="remoto.txt", fk=crypto.generate_file_key(),
                                kek=kr.kek, device="d", content_hash=B, size=1, blocks=[])

        eng = SyncEngine(idx)
        estados = {fs.vault_path: fs.status for fs in eng.status(scanned, manifest)}
        assert estados == {
            "comum.txt": FileStatus.IN_SYNC,
            "novo.txt": FileStatus.LOCAL_ONLY,
            "remoto.txt": FileStatus.SERVER_ONLY,
        }


def test_status_sem_manifesto_tudo_local_only(tmp_path):
    with Index(tmp_path / "i.db") as idx:
        scanned = {"a.txt": _scanned("a.txt", A)}
        eng = SyncEngine(idx)
        estados = {fs.vault_path: fs.status for fs in eng.status(scanned, None)}
        assert estados == {"a.txt": FileStatus.LOCAL_ONLY}
