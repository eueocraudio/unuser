"""Testes do armazenamento cofre-cego."""

import pytest

from server.storage import (
    BlindStorage, ConflictError, InvalidIdError, NotFoundError, StorageError,
)

BID = "b:" + "ab" * 32          # block_id válido (64 hex)
BID2 = "b:" + "cd" * 32


@pytest.fixture
def store(tmp_path):
    return BlindStorage(tmp_path / "vault")


# --- blobs ------------------------------------------------------------------

def test_put_get_has_blob(store):
    assert not store.has_blob(BID)
    store.put_blob(BID, b"cifrado")
    assert store.has_blob(BID)
    assert store.get_blob(BID) == b"cifrado"


def test_put_blob_idempotente(store):
    store.put_blob(BID, b"primeiro")
    store.put_blob(BID, b"segundo")  # endereçado por conteúdo: ignora
    assert store.get_blob(BID) == b"primeiro"


def test_get_blob_inexistente(store):
    with pytest.raises(NotFoundError):
        store.get_blob(BID)


def test_block_id_invalido_rejeitado(store):
    for ruim in ("b:xyz", "../escapar", "b:" + "ab" * 31, "abc", "b:AB" + "00" * 31):
        with pytest.raises(InvalidIdError):
            store.has_blob(ruim)


def test_delete_e_list_blobs(store):
    store.put_blob(BID, b"x")
    store.put_blob(BID2, b"y")
    assert store.list_blobs() == sorted([BID, BID2])
    store.delete_blob(BID)
    assert store.list_blobs() == [BID2]
    store.delete_blob(BID)  # missing_ok


# --- manifesto / CAS --------------------------------------------------------

def test_manifesto_inicial_vazio(store):
    assert store.manifest_version() == 0
    assert store.get_manifest() is None


def test_put_get_manifest(store):
    store.put_manifest(expected_version=0, new_version=1, blob=b"blob-cifrado-1")
    assert store.manifest_version() == 1
    assert store.get_manifest() == (1, b"blob-cifrado-1")


def test_cas_rejeita_versao_esperada_errada(store):
    store.put_manifest(0, 1, b"v1")
    with pytest.raises(ConflictError) as ei:
        store.put_manifest(expected_version=0, new_version=2, blob=b"v2")  # baseou no obsoleto
    assert ei.value.current == 1
    assert ei.value.expected == 0
    assert store.get_manifest() == (1, b"v1")  # inalterado


def test_cas_aceita_sequencia_correta(store):
    store.put_manifest(0, 1, b"v1")
    store.put_manifest(1, 2, b"v2")
    store.put_manifest(2, 3, b"v3")
    assert store.get_manifest() == (3, b"v3")


def test_new_version_precisa_crescer(store):
    store.put_manifest(0, 1, b"v1")
    with pytest.raises(StorageError):
        store.put_manifest(expected_version=1, new_version=1, blob=b"igual")


def test_persistencia_em_disco(tmp_path):
    BlindStorage(tmp_path / "v").put_manifest(0, 1, b"persistido")
    s2 = BlindStorage(tmp_path / "v")
    assert s2.get_manifest() == (1, b"persistido")


# --- uso de disco -----------------------------------------------------------

def test_disk_usage_reporta_disco_e_cofre(store):
    store.put_blob(BID, b"x" * 100)
    store.put_blob(BID2, b"y" * 50)
    store.put_manifest(0, 1, b"manifesto")
    u = store.disk_usage()
    # disco físico: total >= used >= 0 e free coerente
    assert u["disk_total"] > 0 and 0 <= u["disk_used"] <= u["disk_total"]
    assert u["disk_free"] >= 0
    # cofre: 2 blobs e tamanho >= soma dos conteúdos
    assert u["blob_count"] == 2
    assert u["vault_bytes"] >= 150


# --- export / import (backup) -----------------------------------------------

def test_export_lista_e_restaura(store):
    store.put_blob(BID, b"conteudo-cifrado")
    store.put_manifest(0, 1, b"manifesto-1")

    info = store.export()
    assert info["name"].startswith("unuser-backup-") and info["name"].endswith(".tar")
    assert info["size"] > 0
    assert [b["name"] for b in store.list_backups()] == [info["name"]]

    # muda o estado depois do backup
    store.put_blob(BID2, b"novo")
    store.put_manifest(1, 2, b"manifesto-2")
    assert store.get_manifest() == (2, b"manifesto-2")

    # restaurar volta ao estado do backup (manifesto v1, sem o BID2)
    version = store.restore(info["name"])
    assert version == 1
    assert store.get_manifest() == (1, b"manifesto-1")
    assert store.get_blob(BID) == b"conteudo-cifrado"
    assert not store.has_blob(BID2)


def test_export_nao_inclui_backups_antigos(store):
    store.put_blob(BID, b"a")
    primeiro = store.export()
    segundo = store.export()
    # o 2º backup não engole o 1º (backups/ fica fora do tar): tamanhos próximos
    assert segundo["size"] < primeiro["size"] * 3
    assert {primeiro["name"], segundo["name"]} <= {b["name"] for b in store.list_backups()}


def test_restore_nome_invalido_ou_inexistente(store):
    with pytest.raises(InvalidIdError):
        store.restore("../etc/passwd")
    with pytest.raises(InvalidIdError):
        store.restore("qualquer.tar")
    with pytest.raises(NotFoundError):
        store.restore("unuser-backup-20200101-000000.tar")


def test_backups_dir_configuravel(tmp_path):
    """backups_dir aponta p/ mídia secundária (UNUSERD_BACKUPS): o .tar nasce lá, não no storage."""
    media = tmp_path / "midia-secundaria"
    s = BlindStorage(tmp_path / "vault", backups_dir=media)
    s.put_blob(BID, b"x")
    info = s.export()
    assert (media / info["name"]).exists()
    assert not (tmp_path / "vault" / "backups").exists()
