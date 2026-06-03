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
