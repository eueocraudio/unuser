"""Índice local do cliente (SQLite).

Guarda o estado conhecido de cada arquivo do cofre — caminho, tamanho, mtime,
hash de conteúdo e a lista ordenada de ``block_id`` da versão atual. Serve para:

* detectar mudanças rápido (compara mtime/size; só re-hashea se mudou);
* saber quais blocos o cliente já conhece (para não re-enviar).

O histórico completo de versões vive no manifesto (no servidor); este índice é a
visão local e descartável — pode ser reconstruído a partir do disco + manifesto.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    file_id    TEXT PRIMARY KEY,
    path       TEXT NOT NULL UNIQUE,
    size       INTEGER NOT NULL,
    mtime      REAL NOT NULL,
    hash       TEXT NOT NULL,
    mode       TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS file_blocks (
    file_id  TEXT NOT NULL,
    seq      INTEGER NOT NULL,
    block_id TEXT NOT NULL,
    length   INTEGER NOT NULL,
    PRIMARY KEY (file_id, seq),
    FOREIGN KEY (file_id) REFERENCES files(file_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS blocks (
    block_id TEXT PRIMARY KEY,
    length   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_file_blocks_block ON file_blocks(block_id);
"""


@dataclass
class FileRecord:
    file_id: str
    path: str
    size: int
    mtime: float
    hash: str
    mode: str | None = None


@dataclass(frozen=True)
class BlockRef:
    block_id: str
    length: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Index:
    """Acesso ao índice SQLite. Use como context manager ou chame ``close()``."""

    def __init__(self, db_path):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)

    def __enter__(self) -> "Index":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    # --- arquivos -----------------------------------------------------------

    def upsert_file(self, rec: FileRecord) -> None:
        """Insere ou atualiza o registro de um arquivo (chave: ``path``)."""
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO files (file_id, path, size, mtime, hash, mode, updated_at)
                VALUES (:file_id, :path, :size, :mtime, :hash, :mode, :updated_at)
                ON CONFLICT(path) DO UPDATE SET
                    file_id    = excluded.file_id,
                    size       = excluded.size,
                    mtime      = excluded.mtime,
                    hash       = excluded.hash,
                    mode       = excluded.mode,
                    updated_at = excluded.updated_at
                """,
                {
                    "file_id": rec.file_id, "path": rec.path, "size": rec.size,
                    "mtime": rec.mtime, "hash": rec.hash, "mode": rec.mode,
                    "updated_at": _now(),
                },
            )

    def get_file(self, path: str) -> FileRecord | None:
        row = self.conn.execute(
            "SELECT file_id, path, size, mtime, hash, mode FROM files WHERE path = ?",
            (path,),
        ).fetchone()
        return self._to_record(row)

    def get_file_by_id(self, file_id: str) -> FileRecord | None:
        row = self.conn.execute(
            "SELECT file_id, path, size, mtime, hash, mode FROM files WHERE file_id = ?",
            (file_id,),
        ).fetchone()
        return self._to_record(row)

    def all_files(self) -> list[FileRecord]:
        rows = self.conn.execute(
            "SELECT file_id, path, size, mtime, hash, mode FROM files ORDER BY path"
        ).fetchall()
        return [self._to_record(r) for r in rows]

    def remove_file(self, path: str) -> None:
        """Remove o arquivo e seus blocos (cascade)."""
        with self.conn:
            self.conn.execute("DELETE FROM files WHERE path = ?", (path,))

    @staticmethod
    def _to_record(row) -> FileRecord | None:
        if row is None:
            return None
        return FileRecord(
            file_id=row["file_id"], path=row["path"], size=row["size"],
            mtime=row["mtime"], hash=row["hash"], mode=row["mode"],
        )

    # --- blocos -------------------------------------------------------------

    def set_blocks(self, file_id: str, blocks) -> None:
        """Define a lista ordenada de blocos de um arquivo (substitui a anterior).

        ``blocks`` é um iterável de objetos com ``block_id``/``length`` ou de
        tuplas ``(block_id, length)``.
        """
        rows = [self._block_pair(b) for b in blocks]
        with self.conn:
            self.conn.execute("DELETE FROM file_blocks WHERE file_id = ?", (file_id,))
            self.conn.executemany(
                "INSERT INTO file_blocks (file_id, seq, block_id, length) VALUES (?, ?, ?, ?)",
                [(file_id, i, bid, length) for i, (bid, length) in enumerate(rows)],
            )
            self.conn.executemany(
                "INSERT INTO blocks (block_id, length) VALUES (?, ?) "
                "ON CONFLICT(block_id) DO NOTHING",
                rows,
            )

    def get_blocks(self, file_id: str) -> list[BlockRef]:
        rows = self.conn.execute(
            "SELECT block_id, length FROM file_blocks WHERE file_id = ? ORDER BY seq",
            (file_id,),
        ).fetchall()
        return [BlockRef(r["block_id"], r["length"]) for r in rows]

    def has_block(self, block_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM blocks WHERE block_id = ?", (block_id,)
        ).fetchone()
        return row is not None

    @staticmethod
    def _block_pair(b) -> tuple[str, int]:
        if isinstance(b, tuple):
            return b[0], b[1]
        return b.block_id, b.length

    # --- metadados ----------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default
