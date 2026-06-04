"""Carregamento da configuração local do cliente (seção 9 da especificação).

Um arquivo por responsabilidade, cada um no formato que combina com ela:

* ``~/.env`` — **conexão** (escalares ``CHAVE=VALOR``): modo IP/Tor, host, porta, onion,
  socks, e o id do cofre. Vira :class:`ConnConfig`.
* ``~/.config/unuser/dirs.json`` — **o que vai no cofre**: diretórios recursivos + itens
  avulsos. Vira :class:`ContentConfig`.
* ``~/.unuserignore`` — **o que NÃO vai** (globs). Já tratado em :mod:`scanner`.

Nenhum deles contém segredos (passphrase/keyfile ficam de fora — §4.1).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import scanner
from .actions import PathResolver
from .transport import VaultClient

_COMMENT = re.compile(r"\s+#.*$")   # comentário inline: " # ..." (preserva 'a#b')


# --- conexão (~/.env) -------------------------------------------------------

@dataclass
class ConnConfig:
    vault_id: str = "v:default"
    mode: str = "direct"               # direct | tor
    direct_host: str = "127.0.0.1"
    direct_port: int = 8443
    tor_onion: str = ""
    tor_port: int = 8443
    tor_socks: str = "127.0.0.1:9050"

    @classmethod
    def from_mapping(cls, m: dict[str, str]) -> "ConnConfig":
        def get(k, default): return m.get(k, default)
        return cls(
            vault_id=get("UNUSER_VAULT_ID", "v:default"),
            mode=get("UNUSER_CONN_MODE", "direct"),
            direct_host=get("UNUSER_DIRECT_HOST", "127.0.0.1"),
            direct_port=int(get("UNUSER_DIRECT_PORT", "8443")),
            tor_onion=get("UNUSER_TOR_ONION", ""),
            tor_port=int(get("UNUSER_TOR_PORT", "8443")),
            tor_socks=get("UNUSER_TOR_SOCKS", "127.0.0.1:9050"),
        )

    @classmethod
    def from_env(cls, path) -> "ConnConfig":
        """Lê um arquivo ``CHAVE=VALOR`` (ignora linhas vazias e comentários)."""
        m: dict[str, str] = {}
        p = Path(path)
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                m[key.strip()] = _COMMENT.sub("", value).strip()
        return cls.from_mapping(m)

    def socks(self) -> tuple[str, int]:
        """(host, port) do proxy SOCKS5, parseado de ``UNUSER_TOR_SOCKS`` (host:porta)."""
        host, _, port = self.tor_socks.partition(":")
        return host or "127.0.0.1", int(port or "9050")

    def make_client(self, *, ssl_context=None, timeout: float = 30.0) -> VaultClient:
        """Constrói o :class:`VaultClient` conforme o modo de conexão."""
        if self.mode == "direct":
            return VaultClient(self.direct_host, self.direct_port,
                               ssl_context=ssl_context, timeout=timeout)
        if self.mode == "tor":
            # Conecta ao .onion ATRAVÉS do SOCKS5 do Tor; o mTLS roda dentro do túnel.
            return VaultClient(self.tor_onion, self.tor_port, ssl_context=ssl_context,
                               socks_proxy=self.socks(), timeout=timeout)
        raise ValueError(f"modo de conexão desconhecido: {self.mode!r}")


# --- conteúdo (~/.config/unuser/dirs.json) ----------------------------------

@dataclass
class ContentConfig:
    default_dirs: list[tuple[str, bool]] = field(default_factory=list)
    extra_items: list[str] = field(default_factory=list)
    source: Path | None = None         # de onde foi carregado (para regravar)

    @classmethod
    def from_json(cls, path) -> "ContentConfig":
        p = Path(path)
        if not p.exists():
            return cls(source=p)
        d = json.loads(p.read_text(encoding="utf-8"))
        dirs = [(e["path"], bool(e.get("recursive", True)))
                for e in d.get("default_dirs", [])]
        return cls(default_dirs=dirs, extra_items=list(d.get("extra_items", [])), source=p)

    def path_resolver(self) -> PathResolver:
        return PathResolver(self.default_dirs, self.extra_items)

    def scan(self, *, ignore_path=None) -> dict[str, scanner.ScannedFile]:
        ignore = scanner.IgnoreRules.load(ignore_path)
        return scanner.scan(self.default_dirs, self.extra_items, ignore=ignore)

    # --- mutação + persistência -----------------------------------------

    def save(self) -> None:
        """Regrava o ``dirs.json`` (em :attr:`source`) com o estado atual."""
        if self.source is None:
            raise ValueError("ContentConfig sem 'source' — não sei onde gravar")
        out = {
            "default_dirs": [{"path": p, "recursive": r} for p, r in self.default_dirs],
            "extra_items": list(self.extra_items),
        }
        self.source.parent.mkdir(parents=True, exist_ok=True)
        self.source.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    def add_dir(self, path, *, recursive: bool = True) -> bool:
        """Adiciona uma pasta sincronizada (idempotente) e persiste. True se era nova."""
        path = str(Path(path))
        known = {Path(p).resolve() for p, _ in self.default_dirs}
        if Path(path).resolve() in known:
            return False
        self.default_dirs.append((path, recursive))
        self.save()
        return True

    def add_item(self, path) -> bool:
        """Adiciona um arquivo avulso (sincroniza só ele) e persiste. True se era novo."""
        path = str(Path(path))
        if Path(path).resolve() in {Path(i).resolve() for i in self.extra_items}:
            return False
        self.extra_items.append(path)
        self.save()
        return True

    def remove_dir(self, path) -> bool:
        target = Path(path).resolve()
        kept = [(p, r) for p, r in self.default_dirs if Path(p).resolve() != target]
        if len(kept) == len(self.default_dirs):
            return False
        self.default_dirs = kept
        self.save()
        return True

    def remove_item(self, path) -> bool:
        target = Path(path).resolve()
        kept = [i for i in self.extra_items if Path(i).resolve() != target]
        if len(kept) == len(self.extra_items):
            return False
        self.extra_items = kept
        self.save()
        return True

    def resolve_or_register(self, local_path, *, home=None) -> str:
        """Mapeia ``local_path`` → *vault path*, registrando a pasta se preciso.

        Se o arquivo já está sob uma raiz/avulso conhecido, devolve o vault path direto.
        Senão, e estando **dentro de ``~/``**, registra automaticamente (e persiste): a
        **pasta** do arquivo vira raiz sincronizada — exceto se o arquivo estiver **direto
        em ``~/``**, caso em que ele é adicionado como **item avulso** (não sincronizamos a
        home inteira, que traria configs/segredos/caches). Fora de ``~/`` → ``ValueError``.
        """
        home = Path(home or Path.home()).resolve()
        p = Path(local_path).resolve()
        vp = self.path_resolver().vault_path_for(p)
        if vp is not None:
            return vp
        if p != home and home not in p.parents:
            raise ValueError(f"{p}: fora de {home} — não dá para sincronizar automaticamente")
        if p.parent == home:
            self.add_item(p)                 # arquivo solto no ~/ → só ele, não a home toda
        else:
            self.add_dir(p.parent)           # registra a pasta do arquivo como raiz
        vp = self.path_resolver().vault_path_for(p)
        if vp is None:                       # não deveria acontecer após registrar
            raise ValueError(f"{p}: falha ao registrar no cofre")
        return vp
