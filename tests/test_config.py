"""Testes do carregamento de configuração (§9): .env e dirs.json."""

import pytest

from client.config import ConnConfig, ContentConfig
from client.transport import VaultClient


# --- ~/.env (conexão) -------------------------------------------------------

def test_conn_from_env_parseia_e_ignora_comentarios(tmp_path):
    (tmp_path / ".env").write_text(
        "# conexao\n"
        "UNUSER_VAULT_ID=v:abc\n"
        "UNUSER_CONN_MODE=direct            # direct | tor\n"
        "UNUSER_DIRECT_HOST=192.168.0.10\n"
        "UNUSER_DIRECT_PORT=8443\n"
        "\n"
    )
    c = ConnConfig.from_env(tmp_path / ".env")
    assert c.vault_id == "v:abc"
    assert c.mode == "direct"                  # comentário inline removido
    assert c.direct_host == "192.168.0.10"
    assert c.direct_port == 8443               # convertido para int


def test_conn_defaults_quando_arquivo_falta(tmp_path):
    c = ConnConfig.from_env(tmp_path / "nao-existe.env")
    assert c.mode == "direct" and c.direct_port == 8443


def test_make_client_direct_e_tor():
    assert isinstance(ConnConfig(mode="direct").make_client(), VaultClient)
    with pytest.raises(NotImplementedError):       # SOCKS5/Tor é Fase 5
        ConnConfig(mode="tor").make_client()


# --- ~/.config/unuser/dirs.json (conteúdo) ----------------------------------

def test_content_from_json(tmp_path):
    (tmp_path / "dirs.json").write_text(
        '{"default_dirs":[{"path":"/home/u/Documentos","recursive":true},'
        '{"path":"/home/u/Projetos","recursive":false}],'
        '"extra_items":["/home/u/notas/ideias.md"]}'
    )
    cc = ContentConfig.from_json(tmp_path / "dirs.json")
    assert cc.default_dirs == [("/home/u/Documentos", True), ("/home/u/Projetos", False)]
    assert cc.extra_items == ["/home/u/notas/ideias.md"]


def test_content_vazio_quando_falta(tmp_path):
    cc = ContentConfig.from_json(tmp_path / "nada.json")
    assert cc.default_dirs == [] and cc.extra_items == []


def test_content_scan_integra_o_scanner(tmp_path):
    root = tmp_path / "Documentos"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_bytes(b"oi")
    (root / "segredo.key").write_bytes(b"x")      # ignorado pelos defaults
    cc = ContentConfig(default_dirs=[(str(root), True)])
    achados = cc.scan()
    assert set(achados) == {"Documentos/a.txt"}
