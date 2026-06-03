"""Manifesto do cofre (seção 4.4 da especificação).

Dois níveis:

* :class:`VaultManifest` — cabeçalho global: ``format_version``, ``vault_id``,
  ``argon2_salt``, ``kek_epoch``, ``vault_version`` e o índice de arquivos.
* :class:`FileManifest` — por arquivo: ``file_id``, ``path``, ``mode`` (opcional),
  ``wrapped_FK``, ``wrapped_by_epoch`` e o histórico ``versions``.
* :class:`FileVersion` — por versão: ``ts`` (UTC, até o segundo, para desempate),
  ``hash`` (BLAKE3, detecção de modificação), ``size``, ``blocks`` (ids ordenados),
  ``device`` (quem gravou) e ``deleted`` (tombstone do apagar).

O manifesto inteiro é serializado em JSON e cifrado com a ``manifest_key``
(:func:`seal`/:func:`open_sealed`). O ``vault_version`` também viaja em claro,
fora do blob, para o *compare-and-swap* de concorrência no servidor (Fase 4).

A FK é **por arquivo** (não por versão): todas as versões de um arquivo usam a
mesma FK; trocar a chave-mestra só re-embrulha essa FK (:meth:`VaultManifest.rotate_kek`),
sem recriptografar conteúdo.
"""

from __future__ import annotations

import base64
import copy
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from . import crypto

# Versão do ESQUEMA do manifesto (não confundir com `vault_version`, que é o contador
# de mutação para o CAS, nem com o histórico por arquivo em `FileVersion`). Bumpar isto
# sempre que a forma serializada mudar, e registrar a migração correspondente abaixo.
CURRENT_FORMAT_VERSION = 1
_AAD_MANIFEST = b"unuser:v1:manifest"


# --- versionamento de formato e migração ------------------------------------

class ManifestFormatError(Exception):
    """Problema com a versão de formato do manifesto."""


class UnsupportedManifestVersion(ManifestFormatError):
    """Manifesto gravado por uma versão de software mais nova do que esta entende."""

    def __init__(self, found: int, supported: int):
        super().__init__(
            f"manifesto em formato v{found}, mas este software entende só até v{supported}; "
            "atualize o unuser para abri-lo"
        )
        self.found = found
        self.supported = supported


class MigrationError(ManifestFormatError):
    """Falta um passo de migração para chegar do formato lido ao atual."""


# Registro de migrações: ``_MIGRATIONS[n]`` recebe o dict cru no formato ``n`` e devolve
# o dict no formato ``n+1`` (só reformata os dados — o carimbo de ``format_version`` é
# aplicado por :func:`migrate`). Para introduzir um formato v2, escreva a função que
# converte v1->v2, registre-a aqui como ``_MIGRATIONS[1] = ...`` e suba
# ``CURRENT_FORMAT_VERSION`` para 2.
_MIGRATIONS: dict[int, Callable[[dict], dict]] = {}


def _detect_version(raw: dict) -> int:
    """Versão de formato de um dict cru. Manifestos legados (sem o campo) são v1."""
    return int(raw.get("format_version", 1))


def migrate(raw: dict, *, target: int = CURRENT_FORMAT_VERSION,
            migrations: dict[int, Callable[[dict], dict]] = _MIGRATIONS) -> dict:
    """Eleva um manifesto cru até a versão ``target``, aplicando as migrações em cadeia.

    * Versão lida == alvo: devolve uma cópia inalterada (caminho comum).
    * Versão lida  > alvo: :class:`UnsupportedManifestVersion` (arquivo de software novo).
    * Versão lida  < alvo: aplica ``migrations[v]`` para cada ``v`` até o alvo; se faltar
      um passo, :class:`MigrationError`.

    Não muta o ``raw`` recebido (trabalha sobre uma cópia profunda).
    """
    v = _detect_version(raw)
    if v > target:
        raise UnsupportedManifestVersion(v, target)
    raw = copy.deepcopy(raw)
    while v < target:
        step = migrations.get(v)
        if step is None:
            raise MigrationError(f"sem migração de formato v{v} -> v{v + 1}")
        raw = step(raw)
        raw["format_version"] = v + 1   # carimbo canônico: a migração só cuida dos dados
        v += 1
    return raw


# --- utilidades -------------------------------------------------------------

def now_ts() -> str:
    """Carimbo UTC ``YYYY-MM-DD HH:MM:SS`` — largura fixa, ordena lexicograficamente."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def generate_file_id() -> str:
    return "f:" + os.urandom(8).hex()


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def _block_ids(blocks) -> list[str]:
    """Normaliza para lista de ids: aceita str ou objeto com ``.block_id``."""
    out = []
    for b in blocks:
        out.append(b if isinstance(b, str) else b.block_id)
    return out


# --- modelo -----------------------------------------------------------------

@dataclass
class FileVersion:
    ts: str
    device: str
    hash: str | None = None
    size: int | None = None
    blocks: list[str] = field(default_factory=list)
    deleted: bool = False

    def to_dict(self) -> dict:
        if self.deleted:
            return {"ts": self.ts, "device": self.device, "deleted": True}
        return {
            "ts": self.ts, "device": self.device, "hash": self.hash,
            "size": self.size, "blocks": self.blocks,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FileVersion":
        return cls(
            ts=d["ts"], device=d["device"], hash=d.get("hash"),
            size=d.get("size"), blocks=list(d.get("blocks", [])),
            deleted=bool(d.get("deleted", False)),
        )


@dataclass
class FileManifest:
    file_id: str
    path: str
    wrapped_fk: str            # FK embrulhada pela KEK (base64)
    wrapped_by_epoch: int
    versions: list[FileVersion] = field(default_factory=list)
    mode: str | None = None

    @property
    def current(self) -> FileVersion:
        return self.versions[-1]

    @property
    def deleted(self) -> bool:
        return bool(self.versions) and self.versions[-1].deleted

    def to_dict(self) -> dict:
        d = {
            "file_id": self.file_id, "path": self.path,
            "wrapped_fk": self.wrapped_fk, "wrapped_by_epoch": self.wrapped_by_epoch,
            "versions": [v.to_dict() for v in self.versions],
        }
        if self.mode is not None:
            d["mode"] = self.mode
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "FileManifest":
        return cls(
            file_id=d["file_id"], path=d["path"], wrapped_fk=d["wrapped_fk"],
            wrapped_by_epoch=d["wrapped_by_epoch"],
            versions=[FileVersion.from_dict(v) for v in d.get("versions", [])],
            mode=d.get("mode"),
        )


@dataclass
class VaultManifest:
    vault_id: str
    argon2_salt: str           # base64
    kek_epoch: int = 1
    vault_version: int = 0
    files: dict[str, FileManifest] = field(default_factory=dict)
    format_version: int = CURRENT_FORMAT_VERSION

    # --- construção -----------------------------------------------------

    @classmethod
    def new(cls, vault_id: str, salt: bytes, *, kek_epoch: int = 1) -> "VaultManifest":
        return cls(vault_id=vault_id, argon2_salt=_b64e(salt), kek_epoch=kek_epoch)

    @property
    def salt(self) -> bytes:
        return _b64d(self.argon2_salt)

    # --- consultas ------------------------------------------------------

    def file_by_path(self, path: str) -> FileManifest | None:
        for fm in self.files.values():
            if fm.path == path:
                return fm
        return None

    def file_key(self, fm: FileManifest, kek: bytes) -> bytes:
        """Desembrulha a FK do arquivo com a KEK."""
        return crypto.unwrap_file_key(kek, _b64d(fm.wrapped_fk))

    # --- mutações -------------------------------------------------------

    def record_version(self, *, path: str, fk: bytes, kek: bytes, device: str,
                        content_hash: str, size: int, blocks, mode: str | None = None,
                        ts: str | None = None) -> FileManifest:
        """Registra uma nova versão de ``path`` (cria o arquivo se for novo)."""
        ts = ts or now_ts()
        version = FileVersion(ts=ts, device=device, hash=content_hash,
                              size=size, blocks=_block_ids(blocks))
        fm = self.file_by_path(path)
        if fm is None:
            fm = FileManifest(
                file_id=generate_file_id(), path=path,
                wrapped_fk=_b64e(crypto.wrap_file_key(kek, fk)),
                wrapped_by_epoch=self.kek_epoch, versions=[version], mode=mode,
            )
            self.files[fm.file_id] = fm
        else:
            fm.versions.append(version)
            if mode is not None:
                fm.mode = mode
        self.vault_version += 1
        return fm

    def mark_deleted(self, *, path: str, device: str, ts: str | None = None) -> FileManifest:
        """Apaga (tombstone): mantém o histórico, marca a versão atual como deletada."""
        fm = self.file_by_path(path)
        if fm is None:
            raise KeyError(f"arquivo inexistente: {path}")
        fm.versions.append(FileVersion(ts=ts or now_ts(), device=device, deleted=True))
        self.vault_version += 1
        return fm

    def rotate_kek(self, old_kek: bytes, new_kek: bytes, new_epoch: int) -> None:
        """Re-embrulha todas as FKs com a nova KEK — sem tocar no conteúdo (4.2)."""
        if new_epoch <= self.kek_epoch:
            raise ValueError("new_epoch deve ser maior que o epoch atual")
        # Computa tudo ANTES de mutar: se um unwrap falhar (wrapped_fk corrompida →
        # InvalidTag), o manifesto fica intacto, preservando o invariante
        # wrapped_by_epoch ↔ kek_epoch (nada de estado meio-rotacionado).
        rewrapped = []
        for fm in self.files.values():
            fk = crypto.unwrap_file_key(old_kek, _b64d(fm.wrapped_fk))
            rewrapped.append((fm, _b64e(crypto.wrap_file_key(new_kek, fk))))
        for fm, new_wrapped in rewrapped:
            fm.wrapped_fk = new_wrapped
            fm.wrapped_by_epoch = new_epoch
        self.kek_epoch = new_epoch
        self.vault_version += 1

    # --- serialização ---------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "format_version": self.format_version, "vault_id": self.vault_id,
            "argon2_salt": self.argon2_salt, "kek_epoch": self.kek_epoch,
            "vault_version": self.vault_version,
            "files": {fid: fm.to_dict() for fid, fm in self.files.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "VaultManifest":
        # Ponto único de leitura: migra qualquer formato antigo até o atual antes de
        # reconstruir. Versões futuras (desconhecidas) levantam UnsupportedManifestVersion.
        d = migrate(d)
        return cls(
            vault_id=d["vault_id"], argon2_salt=d["argon2_salt"],
            kek_epoch=d["kek_epoch"], vault_version=d["vault_version"],
            files={fid: FileManifest.from_dict(fm) for fid, fm in d.get("files", {}).items()},
            format_version=d.get("format_version", CURRENT_FORMAT_VERSION),
        )


# --- (de)serialização e cifragem -------------------------------------------

def dumps(vault: VaultManifest) -> bytes:
    """JSON canônico em UTF-8 (chaves ordenadas, sem espaços supérfluos)."""
    return json.dumps(vault.to_dict(), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def loads(data: bytes) -> VaultManifest:
    return VaultManifest.from_dict(json.loads(data.decode("utf-8")))


def seal(vault: VaultManifest, manifest_key: bytes) -> bytes:
    """Serializa e cifra o manifesto com a manifest_key (o que vai ao servidor)."""
    return crypto.encrypt(manifest_key, dumps(vault), aad=_AAD_MANIFEST)


def open_sealed(blob: bytes, manifest_key: bytes) -> VaultManifest:
    """Decifra e desserializa o manifesto. Levanta InvalidTag se a chave/AAD não baterem."""
    return loads(crypto.decrypt(manifest_key, blob, aad=_AAD_MANIFEST))
