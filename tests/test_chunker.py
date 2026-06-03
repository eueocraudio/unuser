"""Testes do chunker (content-defined chunking)."""

import random

import pytest

from unuser import chunker, crypto

SMALL = dict(min_size=64, avg_size=256, max_size=1024)


def _data(n: int, seed: int = 0) -> bytes:
    return random.Random(seed).randbytes(n)


def test_reassembly_reproduz_o_original():
    for n in (0, 10, 100, 1000, 50_000):
        data = _data(n, seed=n)
        recomposto = b"".join(c.data for c in chunker.chunk_bytes(data, **SMALL))
        assert recomposto == data


def test_offsets_sao_contiguos():
    data = _data(50_000, seed=1)
    pos = 0
    for c in chunker.chunk_bytes(data, **SMALL):
        assert c.offset == pos
        assert c.length == len(c.data)
        pos += c.length
    assert pos == len(data)


def test_limites_min_max():
    data = _data(50_000, seed=2)
    chunks = list(chunker.chunk_bytes(data, **SMALL))
    for c in chunks[:-1]:  # todos menos o último
        assert SMALL["min_size"] <= c.length <= SMALL["max_size"]
    assert chunks[-1].length <= SMALL["max_size"]


def test_determinismo():
    data = _data(30_000, seed=3)
    a = [(c.offset, c.length) for c in chunker.chunk_bytes(data, **SMALL)]
    b = [(c.offset, c.length) for c in chunker.chunk_bytes(data, **SMALL)]
    assert a == b


def test_block_ids_estaveis_para_o_mesmo_conteudo():
    data = _data(30_000, seed=4)
    key = crypto.generate_file_key()
    ids1 = [b.block_id for b in chunker.split(data, key, **SMALL)]
    ids2 = [b.block_id for b in chunker.split(data, key, **SMALL)]
    assert ids1 == ids2
    assert all(i.startswith("b:") for i in ids1)


def test_edit_local_preserva_a_maioria_dos_blocos():
    """Propriedade do CDC: editar no meio mantém a maior parte dos block_ids."""
    data = _data(200_000, seed=5)
    key = crypto.generate_file_key()
    ids1 = [b.block_id for b in chunker.split(data, key, **SMALL)]

    meio = len(data) // 2
    editado = data[:meio] + b"PEDACO-INSERIDO" + data[meio:]
    ids2 = [b.block_id for b in chunker.split(editado, key, **SMALL)]

    compartilhados = len(set(ids1) & set(ids2))
    # A grande maioria dos blocos deve ser reaproveitada (dedup na transferência).
    assert compartilhados >= 0.8 * len(ids1)


def test_parametros_invalidos():
    with pytest.raises(ValueError):
        list(chunker.chunk_bytes(b"x", min_size=100, avg_size=10, max_size=5))
