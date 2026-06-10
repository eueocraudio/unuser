"""Ações que mutam o cofre (seção 7.2) — o lado "escrita" do fluxo do cliente.

Enquanto :mod:`sync` só classifica (leitura), aqui ficam as operações que transferem
dados e mexem no manifesto:

* **enviar** (ações 1 e 3 da §7.2, "Substituir no servidor" / "Versionar") — por §8 todo
  envio já preserva histórico, então as duas são a mesma operação de armazenamento:
  cifra os blocos, sobe os que faltam e acrescenta uma versão ao manifesto;
* **receber** (ação 2, "Substituir no local") — baixa e remonta o arquivo do cofre;
* **apagar** — tombstone no manifesto (apaga para todos mantendo histórico, §8) e remove
  a cópia local.

O push do manifesto usa **CAS**: se o servidor mudou no meio, o `VaultClient` levanta
``ConflictError`` e cabe a quem chamou recarregar e repetir.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import chunker, crypto, manifest
from .crypto import Keyring
from .index import BlockRef, FileRecord, Index
from .scanner import ScannedFile
from .transport import ConflictError, VaultClient


class IntegrityError(Exception):
    """O conteúdo remontado não bate com o hash registrado no manifesto."""


class NothingToReceive(Exception):
    """O arquivo não existe (ou é um tombstone) no manifesto do servidor."""


class PathResolver:
    """Traduz *vault path* ↔ caminho local, a partir das raízes configuradas (§9.2).

    Como o *vault path* é ``<nome-da-raiz>/<relativo>``, o caminho local é
    ``raiz.parent / vault_path``. Itens avulsos entram pelo basename.

    Quando o prefixo não corresponde a nenhuma raiz/ítem registrado (típico ao
    **receber** numa máquina nova que ainda não configurou as pastas), cai em
    ``~/<vault_path>`` em vez de falhar — assim ``Documents/x`` baixa para
    ``~/Documents/x``. Isso é consistente com o layout usual (raízes direto sob
    ``$HOME``), onde ``raiz.parent / vault_path`` já é ``~/vault_path``.
    """

    def __init__(self, dirs=(), extra_items=(), *, home: Path | None = None):
        self.home = Path(home) if home is not None else Path.home()
        self.roots: dict[str, Path] = {}
        for entry in dirs:
            path, _rec = entry if isinstance(entry, tuple) else (entry, True)
            p = Path(path)
            self.roots[p.name] = p
        self.extras: dict[str, Path] = {Path(i).name: Path(i) for i in extra_items}

    def local_path(self, vault_path: str) -> Path:
        prefix, sep, _rest = vault_path.partition("/")
        if not sep:                                   # item avulso (só basename)
            if vault_path in self.extras:
                return self.extras[vault_path]
            return self.home / vault_path             # fallback: baixa em ~/
        root = self.roots.get(prefix)
        if root is None:
            return self.home / vault_path             # raiz não registrada → ~/<prefixo>/...
        return root.parent / vault_path

    def vault_path_for(self, local_path) -> str | None:
        """Caminho local → *vault path*, ou ``None`` se o arquivo está fora do cofre.

        É o inverso de :meth:`local_path`: usado para "adicionar ao cofre" um arquivo
        escolhido na GUI. Casa o arquivo a uma raiz (``<nome-da-raiz>/<relativo>``) ou a
        um item avulso conhecido (pelo basename). Fora disso, devolve ``None``.
        """
        p = Path(local_path).resolve()
        for name, root in self.roots.items():
            try:
                rel = p.relative_to(Path(root).resolve())
            except ValueError:
                continue
            return f"{name}/{rel.as_posix()}"
        for name, item in self.extras.items():
            if Path(item).resolve() == p:
                return name
        return None


def _apply_mode(path: Path, mode: str | None) -> None:
    if mode:
        try:
            os.chmod(path, int(mode, 8) & 0o7777)
        except (ValueError, OSError):
            pass                                      # melhor-esforço; não é crítico


@dataclass
class VaultSession:
    """Liga keyring + VaultClient + Index + resolver e executa as ações do §7.2."""

    client: VaultClient
    keyring: Keyring
    index: Index
    resolver: PathResolver
    device: str
    vault_id: str = "v:default"
    salt: bytes = b""                                 # usado só para bootstrap de cofre novo
    max_retries: int = 5                              # tentativas de CAS antes de desistir

    # --- manifesto -----------------------------------------------------------

    def _load(self) -> tuple[int, manifest.VaultManifest]:
        """(versão_no_servidor, manifesto). Cofre vazio → manifesto novo na versão 0."""
        version, wire = self.client.get_manifest()
        if not wire:
            salt = self.salt or crypto.generate_salt()
            return 0, manifest.VaultManifest.new(self.vault_id, salt)
        return version, manifest.unpack(wire, self.keyring.manifest_key)

    def _commit(self, expected: int, vault: manifest.VaultManifest) -> None:
        wire = manifest.pack(vault, self.keyring.manifest_key)  # salt em claro + selado
        self.client.put_manifest(expected, vault.vault_version, wire)

    def _apply_with_retry(self, apply) -> manifest.VaultManifest:
        """``load → apply(vault) → CAS``, com **retry automático** em conflito.

        ``apply(vault)`` muta o manifesto recém-carregado e devolve ``True`` se há algo a
        commitar (``False`` = nada a fazer, ex.: apagar o que o servidor já apagou). Em
        :class:`ConflictError` (outro cliente avançou), recarrega o manifesto fresco e
        **reaplica** a mutação — até ``max_retries`` vezes.
        """
        err: ConflictError | None = None
        for _ in range(max(1, self.max_retries)):
            expected, vault = self._load()
            if not apply(vault):
                return vault                          # sem mutação → sem commit
            try:
                self._commit(expected, vault)
                return vault
            except ConflictError as e:
                err = e                               # recarrega e refaz sobre a versão nova
        raise err

    # --- enviar (ações 1 e 3) ------------------------------------------------

    def send_many(self, files: list[ScannedFile], *, progress=None) -> None:
        """Envia vários arquivos num ciclo (load → upload → CAS), com retry em conflito.

        ``progress(feitos, total)`` (opcional) é chamado a cada arquivo enviado — usado pela
        GUI para a barra de progresso. Em retry de CAS o contador reinicia (apply reaplica).
        """
        staged: dict[str, tuple[ScannedFile, list[BlockRef]]] = {}
        total = len(files)

        def apply(vault):
            staged.clear()                            # reaplicado do zero a cada tentativa
            for i, sf in enumerate(files, 1):
                staged[sf.vault_path] = (sf, self._upload_and_record(vault, sf))
                if progress:
                    progress(i, total)
            return True

        vault = self._apply_with_retry(apply)
        for sf, blocks in staged.values():            # base local só após o commit aceitar
            self._record_base(vault, sf, blocks)

    def send(self, scanned: ScannedFile) -> None:
        self.send_many([scanned])

    def _upload_and_record(self, vault: manifest.VaultManifest,
                           sf: ScannedFile) -> list[BlockRef]:
        fm = vault.file_by_path(sf.vault_path)
        if fm is not None and not fm.deleted:
            fk = vault.file_key(fm, self.keyring.kek)  # reaproveita a FK do arquivo
        else:
            fk = crypto.generate_file_key()
        # Streaming: chunka/cifra/envia um bloco por vez; guarda só id+tamanho (não os
        # dados) e hasheia incrementalmente para verificar o content_hash no fim.
        # block_id é POR ARQUIVO (ligado à FK): blocos iguais em arquivos diferentes não
        # colidem no mesmo blob — senão um deles ficaria cifrado com a FK do outro.
        bik = crypto.derive_block_id_key(self.keyring.block_id_key, fk)
        hasher = crypto.content_hasher()
        refs: list[BlockRef] = []
        with open(sf.abspath, "rb") as f:
            for b in chunker.split_stream(f, bik):
                hasher.update(b.data)
                if not self.index.has_block(b.block_id):   # dedup: pula o que já está no cofre
                    self.client.put_blob(b.block_id, crypto.encrypt(fk, b.data))
                refs.append(BlockRef(b.block_id, b.length))
        if "blake3:" + hasher.hexdigest() != sf.hash:
            raise IntegrityError(f"{sf.vault_path}: arquivo mudou durante o envio")
        vault.record_version(
            path=sf.vault_path, fk=fk, kek=self.keyring.kek, device=self.device,
            content_hash=sf.hash, size=sf.size, blocks=refs, mode=sf.mode,
        )
        return refs

    def _record_base(self, vault: manifest.VaultManifest, sf: ScannedFile,
                     blocks: list[BlockRef]) -> None:
        fm = vault.file_by_path(sf.vault_path)
        self.index.upsert_file(FileRecord(
            file_id=fm.file_id, path=sf.vault_path, size=sf.size,
            mtime=sf.mtime, hash=sf.hash, mode=sf.mode,
        ))
        self.index.set_blocks(fm.file_id, blocks)

    # --- receber (ação 2) ----------------------------------------------------

    def receive(self, vault_path: str) -> Path:
        """Baixa, verifica a integridade, grava localmente e atualiza a base. Devolve o path.

        Streaming (simétrico ao envio): decifra/grava um bloco por vez num arquivo
        temporário e hasheia incrementalmente — não segura o arquivo inteiro em memória.
        Só publica o destino final por **rename atômico** depois que o ``content_hash``
        bater; em erro (hash divergente ou rede), o ``.part`` é removido e o arquivo bom
        anterior fica intacto.
        """
        _version, vault = self._load()
        fm = vault.file_by_path(vault_path)
        if fm is None or fm.deleted:
            raise NothingToReceive(vault_path)
        fk = vault.file_key(fm, self.keyring.kek)

        dest = self.resolver.local_path(vault_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.{os.urandom(8).hex()}.part")

        hasher = crypto.content_hasher()
        block_refs: list[tuple[str, int]] = []
        size = 0
        try:
            with open(tmp, "wb") as f:
                for bid in fm.current.blocks:
                    plain = crypto.decrypt(fk, self.client.get_blob(bid))
                    hasher.update(plain)
                    f.write(plain)
                    block_refs.append((bid, len(plain)))
                    size += len(plain)
            if "blake3:" + hasher.hexdigest() != fm.current.hash:
                raise IntegrityError(f"{vault_path}: hash não confere após remontar")
            os.replace(tmp, dest)                     # publica o destino atomicamente
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        _apply_mode(dest, fm.mode)                    # mode é por arquivo (FileManifest)

        self.index.upsert_file(FileRecord(
            file_id=fm.file_id, path=vault_path, size=fm.current.size or size,
            mtime=dest.stat().st_mtime, hash=fm.current.hash, mode=fm.mode,
        ))
        self.index.set_blocks(fm.file_id, block_refs)
        return dest

    # --- apagar --------------------------------------------------------------

    def delete(self, vault_path: str) -> None:
        """Tombstone no servidor (§8: apaga para todos mantendo histórico) + remove local.

        O tombstone usa CAS com retry automático; a remoção local acontece de qualquer
        forma (inclusive se o servidor já tinha apagado).
        """
        def apply(vault):
            fm = vault.file_by_path(vault_path)
            if fm is None or fm.deleted:
                return False                          # servidor já não tem o arquivo
            vault.mark_deleted(path=vault_path, device=self.device)
            return True

        self._apply_with_retry(apply)
        dest = self.resolver.local_path(vault_path)
        try:
            if dest.exists():
                dest.unlink()
        except OSError:
            pass                                      # best-effort: falha ao remover o
                                                      # local não desfaz o tombstone gravado
        self.index.remove_file(vault_path)
