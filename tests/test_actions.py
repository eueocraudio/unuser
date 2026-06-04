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
        ver, wire = self.session.client.get_manifest()
        from client import manifest as m
        return None if not wire else m.unpack(wire, self.session.keyring.manifest_key)

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
        # block_id é por-arquivo (ligado à FK), então recomputa com a mesma chave derivada.
        mani = A.manifest()
        fm = mani.file_by_path("Documentos/a.txt")
        bik = crypto.derive_block_id_key(keyring.block_id_key, mani.file_key(fm, keyring.kek))
        blocos = [b.block_id for b in chunker.split(data, bik)]
        assert all(A.index.has_block(b) for b in blocos)   # base local conhece os blocos
        assert store.list_blobs()                          # e o servidor os guardou


def test_blocos_iguais_em_arquivos_diferentes_nao_corrompem(tmp_path, keyring):
    """Regressão: dois arquivos de conteúdo idêntico compartilhariam o block_id sob uma
    chave global e colapsariam num único blob — cifrado com a FK de só um deles, deixando
    o outro IRRECUPERÁVEL. Com block_id por-arquivo (ligado à FK) cada um tem seu blob."""
    store = BlindStorage(tmp_path / "vault")
    with running(store) as port:
        A = Machine(tmp_path / "A", port, keyring, "pc-A")
        B = Machine(tmp_path / "B", port, keyring, "pc-B")

        data = b"conteudo duplicado " * 500            # mesmo conteúdo nos dois arquivos
        A.session.send(A.write("a.txt", data))
        A.session.send(A.write("b.txt", data))

        # FKs distintas ⇒ block_ids (e blobs) distintos, sem colisão entre arquivos.
        mani = A.manifest()
        fa = mani.file_by_path("Documentos/a.txt")
        fb = mani.file_by_path("Documentos/b.txt")
        assert set(fa.current.blocks).isdisjoint(fb.current.blocks)

        # B remonta AMBOS sem erro (antes da correção, o segundo dava IntegrityError).
        assert B.session.receive("Documentos/a.txt").read_bytes() == data
        assert B.session.receive("Documentos/b.txt").read_bytes() == data


def test_vault_path_for_mapeia_inverso(tmp_path):
    """vault_path_for é o inverso de local_path: caminho local → vault path (ou None)."""
    root = tmp_path / "Documentos"
    (root / "sub").mkdir(parents=True)
    dentro = root / "sub" / "x.txt"
    dentro.write_text("oi")
    r = PathResolver([(root, True)])
    assert r.vault_path_for(dentro) == "Documentos/sub/x.txt"
    assert r.vault_path_for(root / "topo.txt") == "Documentos/topo.txt"
    assert r.vault_path_for(tmp_path / "fora.txt") is None     # fora de qualquer raiz


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


# --- retry automático no conflito -------------------------------------------

class _FlakyManifest:
    """Embrulha um VaultClient e falha os N primeiros PUT /manifest com ConflictError
    (sem tocar no servidor), delegando o resto."""

    def __init__(self, real, fails):
        self._real = real
        self._fails = fails
        self.tentativas = 0

    def put_manifest(self, *a, **k):
        self.tentativas += 1
        if self.tentativas <= self._fails:
            raise ConflictError(current=999)
        return self._real.put_manifest(*a, **k)

    def __getattr__(self, name):                           # health/get/put_blob/... → real
        return getattr(self._real, name)


def _session(client, keyring, base, name, **kw):
    root = base / "Documentos"
    root.mkdir(parents=True)
    return root, VaultSession(
        client=client, keyring=keyring, index=Index(base / "i.db"),
        resolver=PathResolver([(root, True)]), device=name, salt=SALT, **kw,
    )


def test_send_refaz_automaticamente_apos_conflito(tmp_path, keyring):
    store = BlindStorage(tmp_path / "vault")
    with running(store) as port:
        flaky = _FlakyManifest(VaultClient("127.0.0.1", port), fails=1)
        root, sess = _session(flaky, keyring, tmp_path / "A", "pc-A")
        (root / "nota.txt").write_bytes(b"conteudo importante")
        sf = scanner.scan([(root, True)])["Documentos/nota.txt"]

        sess.send(sf)                                      # 1º CAS conflita, 2º (auto) passa
        assert flaky.tentativas == 2

        # chegou mesmo ao servidor
        _, wire = VaultClient("127.0.0.1", port).get_manifest()
        from client import manifest as m
        vault = m.unpack(wire, keyring.manifest_key)
        assert vault.file_by_path("Documentos/nota.txt") is not None


def test_send_desiste_apos_max_retries(tmp_path, keyring):
    store = BlindStorage(tmp_path / "vault")
    with running(store) as port:
        flaky = _FlakyManifest(VaultClient("127.0.0.1", port), fails=99)
        root, sess = _session(flaky, keyring, tmp_path / "B", "pc-B", max_retries=3)
        (root / "x.txt").write_bytes(b"y")
        sf = scanner.scan([(root, True)])["Documentos/x.txt"]

        with pytest.raises(ConflictError):
            sess.send(sf)
        assert flaky.tentativas == 3                       # tentou exatamente max_retries


# --- bootstrap cross-máquina pelo salt publicado no manifesto ----------------

def test_maquina_nova_abre_cofre_pelo_salt_publicado(tmp_path):
    """Máquina B, sem conhecer o salt, deriva as MESMAS chaves a partir do salt em claro
    publicado no manifesto e lê o arquivo que A enviou."""
    from client import manifest

    store = BlindStorage(tmp_path / "vault")
    with running(store) as port:
        # Máquina A: gera o salt, deriva as chaves e envia um arquivo.
        salt_a = crypto.generate_salt()
        kr_a = crypto.unlock("senha", b"keyfile", salt_a, **FAST)
        root_a, sess_a = _session(VaultClient("127.0.0.1", port), kr_a, tmp_path / "A", "A")
        sess_a.salt = salt_a
        (root_a / "nota.txt").write_bytes(b"conteudo secreto")
        sess_a.send(scanner.scan([(root_a, True)])["Documentos/nota.txt"])

        # Máquina B: NÃO conhece o salt — pega do manifesto publicado (em claro).
        _, wire = VaultClient("127.0.0.1", port).get_manifest()
        salt_b = manifest.peek_salt(wire)
        assert salt_b == salt_a                            # veio do servidor, sem cópia manual

        kr_b = crypto.unlock("senha", b"keyfile", salt_b, **FAST)
        root_b, sess_b = _session(VaultClient("127.0.0.1", port), kr_b, tmp_path / "B", "B")
        dest = sess_b.receive("Documentos/nota.txt")
        assert dest.read_bytes() == b"conteudo secreto"
