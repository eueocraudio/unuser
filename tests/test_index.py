"""Testes do índice local SQLite."""

import pytest

from client import chunker, crypto
from client.index import BlockRef, FileRecord, Index


@pytest.fixture
def idx(tmp_path):
    with Index(tmp_path / "index.db") as i:
        yield i


def _rec(path="Documentos/a.txt", file_id="f:1", **kw):
    base = dict(file_id=file_id, path=path, size=10, mtime=1.0, hash="blake3:aa", mode="0644")
    base.update(kw)
    return FileRecord(**base)


def test_upsert_e_get(idx):
    rec = _rec()
    idx.upsert_file(rec)
    got = idx.get_file(rec.path)
    assert got == rec
    assert idx.get_file_by_id(rec.file_id) == rec


def test_get_inexistente(idx):
    assert idx.get_file("nao/existe") is None


def test_upsert_no_mesmo_path_atualiza(idx):
    idx.upsert_file(_rec(hash="blake3:old", size=10))
    idx.upsert_file(_rec(hash="blake3:new", size=20))
    got = idx.get_file("Documentos/a.txt")
    assert got.hash == "blake3:new"
    assert got.size == 20
    assert len(idx.all_files()) == 1


def test_all_files_ordenado(idx):
    idx.upsert_file(_rec(path="z.txt", file_id="f:z"))
    idx.upsert_file(_rec(path="a.txt", file_id="f:a"))
    assert [f.path for f in idx.all_files()] == ["a.txt", "z.txt"]


def test_blocos_preservam_ordem(idx):
    idx.upsert_file(_rec())
    refs = [BlockRef("b:1", 100), BlockRef("b:2", 200), BlockRef("b:3", 50)]
    idx.set_blocks("f:1", refs)
    assert idx.get_blocks("f:1") == refs


def test_set_blocks_substitui_anterior(idx):
    idx.upsert_file(_rec())
    idx.set_blocks("f:1", [BlockRef("b:1", 1), BlockRef("b:2", 2)])
    idx.set_blocks("f:1", [BlockRef("b:9", 9)])
    assert idx.get_blocks("f:1") == [BlockRef("b:9", 9)]


def test_has_block(idx):
    idx.upsert_file(_rec())
    idx.set_blocks("f:1", [BlockRef("b:abc", 10)])
    assert idx.has_block("b:abc")
    assert not idx.has_block("b:zzz")


def test_set_blocks_aceita_objetos_do_chunker(idx):
    """Aceita tanto BlockRef/tuplas quanto chunker.Block (tem block_id/length)."""
    idx.upsert_file(_rec())
    blocks = chunker.split(b"conteudo de teste " * 50, crypto.generate_file_key(),
                           min_size=16, avg_size=32, max_size=128)
    idx.set_blocks("f:1", blocks)
    armazenados = idx.get_blocks("f:1")
    assert [b.block_id for b in armazenados] == [b.block_id for b in blocks]


def test_remove_file_apaga_blocos_em_cascata(idx):
    idx.upsert_file(_rec())
    idx.set_blocks("f:1", [BlockRef("b:1", 1)])
    idx.remove_file("Documentos/a.txt")
    assert idx.get_file("Documentos/a.txt") is None
    assert idx.get_blocks("f:1") == []


def test_meta(idx):
    assert idx.get_meta("vault_version") is None
    assert idx.get_meta("vault_version", "0") == "0"
    idx.set_meta("vault_version", "7")
    assert idx.get_meta("vault_version") == "7"
    idx.set_meta("vault_version", "8")
    assert idx.get_meta("vault_version") == "8"


def test_persistencia_em_disco(tmp_path):
    db = tmp_path / "p.db"
    with Index(db) as i:
        i.upsert_file(_rec())
        i.set_blocks("f:1", [BlockRef("b:1", 1)])
        i.set_meta("k", "v")
    with Index(db) as i:  # reabre
        assert i.get_file("Documentos/a.txt") == _rec()
        assert i.get_blocks("f:1") == [BlockRef("b:1", 1)]
        assert i.get_meta("k") == "v"
