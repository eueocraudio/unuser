"""Divisão de arquivos em blocos por conteúdo (content-defined chunking).

Usa um Gear hash para escolher as fronteiras com base no conteúdo, não em
posições fixas. Vantagem: ao editar um trecho do arquivo, só os blocos próximos
mudam — os demais mantêm o mesmo ``block_id`` e não precisam ser retransmitidos
(seção da especificação sobre transferência por blocos).

A tabela do Gear é **determinística** (derivada por SHA-256 de um rótulo fixo),
para que todas as máquinas produzam exatamente as mesmas fronteiras e, portanto,
os mesmos ``block_id`` — condição para a deduplicação funcionar entre clientes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterator

from .crypto import block_id as _block_id

# Parâmetros padrão dos blocos (bytes).
MIN_SIZE = 4 * 1024
AVG_SIZE = 16 * 1024
MAX_SIZE = 64 * 1024

_MASK64 = (1 << 64) - 1


def _build_gear() -> tuple[int, ...]:
    """Tabela Gear determinística (256 valores de 64 bits)."""
    return tuple(
        int.from_bytes(hashlib.sha256(b"unuser-gear-v1" + bytes([i])).digest()[:8], "big")
        for i in range(256)
    )


_GEAR = _build_gear()


def _avg_mask(avg_size: int) -> int:
    bits = max(1, avg_size.bit_length() - 1)
    return (1 << bits) - 1


@dataclass(frozen=True)
class Chunk:
    """Um bloco recortado: posição, tamanho e os bytes."""

    offset: int
    length: int
    data: bytes


@dataclass(frozen=True)
class Block:
    """Bloco já identificado (pronto para o índice/transferência)."""

    block_id: str
    length: int
    data: bytes


def _cut_points(data: bytes, min_size: int, avg_size: int, max_size: int
                ) -> Iterator[tuple[int, int]]:
    if min_size <= 0 or not (min_size <= avg_size <= max_size):
        raise ValueError("exige 0 < min_size <= avg_size <= max_size")

    mask = _avg_mask(avg_size)
    n = len(data)
    start = 0
    while start < n:
        if n - start <= min_size:
            yield (start, n - start)
            break
        end = min(start + max_size, n)
        fp = 0
        cut = end
        j = start
        while j < end:
            fp = ((fp << 1) + _GEAR[data[j]]) & _MASK64
            if (j - start + 1) >= min_size and (fp & mask) == 0:
                cut = j + 1
                break
            j += 1
        yield (start, cut - start)
        start = cut


def chunk_bytes(data: bytes, *, min_size: int = MIN_SIZE, avg_size: int = AVG_SIZE,
                max_size: int = MAX_SIZE) -> Iterator[Chunk]:
    """Recorta ``data`` em blocos. Concatenar os blocos reproduz o original."""
    for off, length in _cut_points(data, min_size, avg_size, max_size):
        yield Chunk(off, length, data[off:off + length])


def chunk_file(path, **kw) -> Iterator[Chunk]:
    """Como :func:`chunk_bytes`, lendo o arquivo (carregado em memória — MVP)."""
    with open(path, "rb") as f:
        data = f.read()
    yield from chunk_bytes(data, **kw)


def split(data: bytes, block_id_key: bytes, **kw) -> list[Block]:
    """Recorta e já calcula o ``block_id`` (HMAC) de cada bloco."""
    return [
        Block(_block_id(block_id_key, c.data), c.length, c.data)
        for c in chunk_bytes(data, **kw)
    ]
