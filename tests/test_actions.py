"""E2E das ações do §7.2 — duas "máquinas" (keyring igual, índices/árvores separados)
conversando por um servidor cofre-cego real."""

import threading
from contextlib import contextmanager

import pytest

from client import chunker, crypto, scanner
from client.actions import NothingToReceive, PathResolver, VaultSession
from client.index import Index
from client.sync import FileStatus, SyncEngine
from client.transport import ConflictError, VaultClient
from server.http_server import make_server
from server.storage import BlindStorage

FAST = dict(time_cost=1, memory_cost=8 * 1024, parallelism=1)
SALT = b"0123456789abcdef"


@contextmanager
def running(storage):
    httpd = make_server(storage)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


@pytest.fixture
def keyring():
    return crypto.unlock("senha", b"keyfile", SALT, **FAST)


class Machine:
    """Uma máquina cliente: raiz local própria, índice próprio, sessão própria."""

    def __init__(self, base, port, keyring, name):
        self.root = base / "Documentos"
        self.root.mkdir(parents=True, exist_ok=True)
        self.index = Index(base / "index.db")
        self.session = VaultSession(
            client=VaultClient("127.0.0.1", port), keyring=keyring, index=self.index,
            resolver=PathResolver([(self.root, True)]), device=name, salt=SALT,
        )

    def write(self, rel, data: bytes):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return scanner.scan([(self.root, True)])[f"Documentos/{rel}"]

    def scan(self):
        return scanner.scan([(self.root, True)])

    def manifest(self):
        ver, blob = self.session.client.get_manifest()
        from client import manifest as m
        return None if not blob else m.open_sealed(blob, self.session.keyring.manifest_key)

    def status(self):
        eng = SyncEngine(self.index)
        return {fs.vault_path: fs.status for fs in eng.status(self.scan(), self.manifest())}


# --- enviar / receber entre máquinas ----------------------------------------

def test_send_recebe_round_trip(tmp_path, keyring):
    store = BlindStorage(tmp_path / "vault")
    with running(store) as port:
        A = Machine(tmp_path / "A", port, keyring, "pc-A")
        B = Machine(tmp_path / "B", port, keyring, "pc-B")

        sf = A.write("nota.txt", b"conteudo importante " * 50)
        assert A.status()["Documentos/nota.txt"] == FileStatus.LOCAL_ONLY
        A.session.send(sf)
        assert A.status()["Documentos/nota.txt"] == FileStatus.IN_SYNC

        # B ainda não tem nada local; vê o arquivo como "só no servidor".
        assert B.status()["Documentos/nota.txt"] == FileStatus.SERVER_ONLY
        dest = B.session.receive("Documentos/nota.txt")
        assert dest.read_bytes() == b"conteudo importante " * 50
        assert B.status()["Documentos/nota.txt"] == FileStatus.IN_SYNC


def test_segunda_versao_propaga(tmp_path, keyring):
    store = BlindStorage(tmp_path / "vault")
    with running(store) as port:
        A = Machine(tmp_path / "A", port, keyring, "pc-A")
        B = Machine(tmp_path / "B", port, keyring, "pc-B")

        A.session.send(A.write("doc.txt", b"v1 " * 100))
        B.session.receive("Documentos/doc.txt")

        sf2 = A.write("doc.txt", b"v2 mudou " * 100)
        assert A.status()["Documentos/doc.txt"] == FileStatus.LOCAL_MODIFIED
        A.session.send(sf2)

        # do ponto de vista de B (local na v1), o servidor está mais novo.
        assert B.status()["Documentos/doc.txt"] == FileStatus.SERVER_MODIFIED
        B.session.receive("Documentos/doc.txt")
        assert (B.root / "doc.txt").read_bytes() == b"v2 mudou " * 100
        assert B.status()["Documentos/doc.txt"] == FileStatus.IN_SYNC


def test_apagar_vira_tombstone_e_remove_local(tmp_path, keyring):
    store = BlindStorage(tmp_path / "vault")
    with running(store) as port:
        A = Machine(tmp_path / "A", port, keyring, "pc-A")
        B = Machine(tmp_path / "B", port, keyring, "pc-B")

        A.session.send(A.write("lixo.txt", b"apagar depois"))
        B.session.receive("Documentos/lixo.txt")

        A.session.delete("Documentos/lixo.txt")
        assert not (A.root / "lixo.txt").exists()
        # histórico preservado: o manifesto mantém o arquivo, com tombstone na ponta.
        fm = A.manifest().file_by_path("Documentos/lixo.txt")
        assert fm.deleted and len(fm.versions) == 2

        # B tenta receber algo que virou tombstone → nada a baixar.
        with pytest.raises(NothingToReceive):
            B.session.receive("Documentos/lixo.txt")


def test_dedup_nao_reenvia_bloco_conhecido(tmp_path, keyring):
    store = BlindStorage(tmp_path / "vault")
    with running(store) as port:
        A = Machine(tmp_path / "A", port, keyring, "pc-A")
        data = b"bloco repetido " * 300
        A.session.send(A.write("a.txt", data))
        blocos = [b.block_id for b in chunker.split(data, keyring.block_id_key)]
        assert all(A.index.has_block(b) for b in blocos)   # base local conhece os blocos
        assert store.list_blobs()                          # e o servidor os guardou


# --- conflito (CAS) ----------------------------------------------------------

def test_push_concorrente_levanta_conflito(tmp_path, keyring):
    store = BlindStorage(tmp_path / "vault")
    with running(store) as port:
        A = Machine(tmp_path / "A", port, keyring, "pc-A")
        B = Machine(tmp_path / "B", port, keyring, "pc-B")

        A.session.send(A.write("x.txt", b"base"))
        B.session.receive("Documentos/x.txt")

        # B carrega o manifesto (fixa expected) ANTES de A publicar de novo.
        expected_B, vault_B = B.session._load()
        sf_b = B.write("x.txt", b"edicao do B")
        B.session._upload_and_record(vault_B, sf_b)

        A.session.send(A.write("x.txt", b"edicao do A"))   # A publica primeiro → servidor avança

        with pytest.raises(ConflictError):                 # B baseou-se em versão obsoleta
            B.session._commit(expected_B, vault_B)
