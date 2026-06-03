"""Varredura local dos diretórios do cofre (seções 7 e 9.3 da especificação).

Percorre as raízes configuradas (recursivas) + itens avulsos, aplica as regras de
exclusão do ``.unuserignore`` (globs estilo ``.gitignore``, com defaults de segurança
embutidos) e devolve, por arquivo, o **caminho no cofre** (`vault path`) e o
``content_hash`` BLAKE3 do conteúdo atual.

Convenção de *vault path* (estável e portável entre máquinas): cada arquivo é nomeado
relativo à raiz que o contém, prefixado pelo **nome** dessa raiz — p.ex. a raiz
``/home/ana/Documentos`` produz ``Documentos/relatorio.txt``. Assim o mesmo arquivo
recebe o mesmo nome em máquinas com ``$HOME`` diferentes. Itens avulsos entram pelo
basename.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

from . import crypto

# Defaults de segurança embutidos (espelham a seção 9.3); o ``.unuserignore`` do usuário
# acrescenta a estes. O próprio arquivo de ignore nunca entra no cofre.
DEFAULT_IGNORE: tuple[str, ...] = (
    ".env", "*.key", "*.pem", ".ssh/", ".gnupg/", "*.tmp", "*.swp", ".git/",
    ".unuserignore",
)


@dataclass(frozen=True)
class ScannedFile:
    vault_path: str   # nome no cofre (relativo à raiz, prefixado pelo nome dela)
    abspath: str      # caminho absoluto no disco local
    size: int
    mtime: float
    mode: str         # permissões octais, ex. "100644"
    hash: str         # content_hash BLAKE3 do conteúdo atual


class IgnoreRules:
    """Subconjunto pragmático das regras do ``.gitignore``.

    Suportado: comentários (``#``)/linhas vazias; padrão terminado em ``/`` casa um
    **diretório** por nome em qualquer nível; demais padrões casam por *glob* tanto no
    nome-base quanto no caminho relativo. Um arquivo é excluído se ele ou qualquer
    diretório ancestral casar.
    """

    def __init__(self, patterns):
        self.dir_patterns: list[str] = []
        self.glob_patterns: list[str] = []
        for raw in patterns:
            p = raw.strip()
            if not p or p.startswith("#"):
                continue
            if p.endswith("/"):
                self.dir_patterns.append(p[:-1])
            else:
                self.glob_patterns.append(p)

    @classmethod
    def load(cls, ignore_path=None, *, defaults: tuple[str, ...] = DEFAULT_IGNORE
             ) -> "IgnoreRules":
        """Carrega os defaults de segurança + as linhas do ``.unuserignore`` (se existir)."""
        patterns = list(defaults)
        if ignore_path is not None:
            p = Path(ignore_path)
            if p.exists():
                patterns.extend(p.read_text(encoding="utf-8").splitlines())
        return cls(patterns)

    def _name_match(self, name: str, patterns) -> bool:
        return any(fnmatch.fnmatch(name, pat) for pat in patterns)

    def matches(self, rel_path: str) -> bool:
        """``rel_path`` é o caminho relativo à raiz (com ``/``)."""
        parts = rel_path.split("/")
        name = parts[-1]
        # qualquer diretório ancestral excluído?
        for seg in parts[:-1]:
            if self._name_match(seg, self.dir_patterns) or \
               self._name_match(seg, self.glob_patterns):
                return True
        # o próprio arquivo: glob no nome-base ou no caminho relativo inteiro
        return (
            self._name_match(name, self.glob_patterns)
            or self._name_match(rel_path, self.glob_patterns)
            or self._name_match(name, self.dir_patterns)
        )


def _file_meta(abspath: Path) -> tuple[int, float, str, str]:
    st = abspath.stat()
    data = abspath.read_bytes()           # MVP: lê inteiro em memória (igual ao chunker)
    mode = oct(st.st_mode)[-6:] if st.st_mode > 0o7777 else format(st.st_mode, "06o")
    return st.st_size, st.st_mtime, mode, crypto.content_hash(data)


def scan_root(root: Path, *, recursive: bool, ignore: IgnoreRules,
              prefix: str | None = None) -> dict[str, ScannedFile]:
    """Varre uma raiz de diretório, devolvendo ``vault_path -> ScannedFile``."""
    root = Path(root)
    prefix = root.name if prefix is None else prefix
    out: dict[str, ScannedFile] = {}
    if not root.is_dir():
        return out
    it = root.rglob("*") if recursive else root.glob("*")
    for p in it:
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if ignore.matches(rel):
            continue
        size, mtime, mode, h = _file_meta(p)
        vault_path = f"{prefix}/{rel}"
        out[vault_path] = ScannedFile(vault_path, str(p), size, mtime, mode, h)
    return out


def scan(dirs, extra_items=(), *, ignore: IgnoreRules | None = None
         ) -> dict[str, ScannedFile]:
    """Varre as raízes (``[(path, recursive), ...]``) + itens avulsos.

    Devolve um mapa ``vault_path -> ScannedFile`` cobrindo todo o conteúdo do cofre.
    """
    ignore = ignore or IgnoreRules.load()
    out: dict[str, ScannedFile] = {}
    for entry in dirs:
        path, recursive = (entry if isinstance(entry, tuple) else (entry, True))
        out.update(scan_root(Path(path), recursive=recursive, ignore=ignore))
    for item in extra_items:
        p = Path(item)
        if p.is_file() and not ignore.matches(p.name):
            size, mtime, mode, h = _file_meta(p)
            out[p.name] = ScannedFile(p.name, str(p), size, mtime, mode, h)
    return out
