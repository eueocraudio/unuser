"""Testes do chunker (content-defined chunking)."""

import io
import random

import pytest

from client import chunker, crypto

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
    with pytest.raises(ValueError):
        list(chunker.chunk_stream(io.BytesIO(b"x"), min_size=100, avg_size=10, max_size=5))


# --- streaming: DEVE ser idêntico à versão em memória -----------------------

class _DripReader:
    """Devolve no máximo ``step`` bytes por read() — força o caminho de leitura parcial."""

    def __init__(self, data: bytes, step: int):
        self._buf = memoryview(data)
        self._pos = 0
        self._step = step

    def read(self, n: int) -> bytes:
        take = min(n, self._step, len(self._buf) - self._pos)
        out = bytes(self._buf[self._pos:self._pos + take])
        self._pos += take
        return out


@pytest.mark.parametrize("n", [0, 1, 63, 64, 65, 256, 1023, 1024, 1025, 4096, 50_000, 123_457])
def test_chunk_stream_identico_ao_chunk_bytes(n):
    data = _data(n, seed=n)
    em_memoria = [(c.offset, c.length, c.data) for c in chunker.chunk_bytes(data, **SMALL)]
    streaming = [(c.offset, c.length, c.data) for c in chunker.chunk_stream(io.BytesIO(data), **SMALL)]
    assert streaming == em_memoria                     # fronteiras byte-a-byte iguais
    assert b"".join(c[2] for c in streaming) == data   # e reconstrói o original


def test_chunk_stream_robusto_a_leitura_parcial():
    """Mesmo com read() devolvendo poucos bytes por vez, as fronteiras não mudam."""
    data = _data(40_000, seed=7)
    base = [c.data for c in chunker.chunk_bytes(data, **SMALL)]
    for step in (1, 3, 17, 1000):
        got = [c.data for c in chunker.chunk_stream(_DripReader(data, step), **SMALL)]
        assert got == base


def test_split_stream_mesmos_block_ids_da_versao_em_memoria():
    data = _data(80_000, seed=9)
    key = crypto.generate_file_key()
    em_memoria = [b.block_id for b in chunker.split(data, key, **SMALL)]
    streaming = [b.block_id for b in chunker.split_stream(io.BytesIO(data), key, **SMALL)]
    assert streaming == em_memoria


def test_chunk_file_e_split_file_streaming(tmp_path):
    data = _data(70_000, seed=11)
    p = tmp_path / "grande.bin"
    p.write_bytes(data)
    key = crypto.generate_file_key()
    assert [c.data for c in chunker.chunk_file(p, **SMALL)] == \
        [c.data for c in chunker.chunk_bytes(data, **SMALL)]
    assert [b.block_id for b in chunker.split_file(p, key, **SMALL)] == \
        [b.block_id for b in chunker.split(data, key, **SMALL)]
