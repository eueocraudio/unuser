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

    def make_client(self, *, ssl_context=None, timeout: float = 30.0) -> VaultClient:
        """Constrói o :class:`VaultClient` conforme o modo de conexão."""
        if self.mode == "direct":
            return VaultClient(self.direct_host, self.direct_port,
                               ssl_context=ssl_context, timeout=timeout)
        if self.mode == "tor":
            # O túnel SOCKS5 do cliente entra na Fase 5 (ver doc/operacao-tor.md).
            raise NotImplementedError("conexão via Tor/SOCKS5 ainda não implementada (Fase 5)")
        raise ValueError(f"modo de conexão desconhecido: {self.mode!r}")


# --- conteúdo (~/.config/unuser/dirs.json) ----------------------------------

@dataclass
class ContentConfig:
    default_dirs: list[tuple[str, bool]] = field(default_factory=list)
    extra_items: list[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, path) -> "ContentConfig":
        p = Path(path)
        if not p.exists():
            return cls()
        d = json.loads(p.read_text(encoding="utf-8"))
        dirs = [(e["path"], bool(e.get("recursive", True)))
                for e in d.get("default_dirs", [])]
        return cls(default_dirs=dirs, extra_items=list(d.get("extra_items", [])))

    def path_resolver(self) -> PathResolver:
        return PathResolver(self.default_dirs, self.extra_items)

    def scan(self, *, ignore_path=None) -> dict[str, scanner.ScannedFile]:
        ignore = scanner.IgnoreRules.load(ignore_path)
        return scanner.scan(self.default_dirs, self.extra_items, ignore=ignore)
