"""Testes do carregamento de configuração (§9): .env e dirs.json."""

from pathlib import Path

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
    direto = ConnConfig(mode="direct", direct_host="10.0.0.1", direct_port=9000).make_client()
    assert isinstance(direto, VaultClient) and direto.socks_proxy is None

    tor = ConnConfig(mode="tor", tor_onion="abc.onion", tor_port=8443,
                     tor_socks="127.0.0.1:9050").make_client()
    assert tor.host == "abc.onion" and tor.port == 8443
    assert tor.socks_proxy == ("127.0.0.1", 9050)   # tunelado pelo SOCKS5 do Tor


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


# --- mutação/persistência e auto-registro de pastas (dirs.json) -------------

def test_add_dir_persiste_e_eh_idempotente(tmp_path):
    cfg_path = tmp_path / ".config" / "unuser" / "dirs.json"
    cfg = ContentConfig.from_json(cfg_path)               # não existe → vazio, com source
    assert cfg.add_dir(tmp_path / "Docs") is True
    assert cfg.add_dir(tmp_path / "Docs") is False        # idempotente
    reread = ContentConfig.from_json(cfg_path)            # persistiu em disco
    assert [Path(p) for p, _ in reread.default_dirs] == [tmp_path / "Docs"]
    assert cfg.remove_dir(tmp_path / "Docs") is True
    assert ContentConfig.from_json(cfg_path).default_dirs == []


def test_resolve_registra_a_pasta_quando_dentro_do_home(tmp_path):
    home = tmp_path / "home"
    proj = home / "Projetos"
    proj.mkdir(parents=True)
    f = proj / "rel.txt"
    f.write_text("x")
    cfg = ContentConfig.from_json(home / ".config" / "unuser" / "dirs.json")

    vp = cfg.resolve_or_register(f, home=home)
    assert vp == "Projetos/rel.txt"                       # vault path pela raiz registrada
    assert any(Path(p).resolve() == proj.resolve() for p, _ in cfg.default_dirs)
    # idempotente: 2º arquivo na mesma pasta não duplica a raiz
    g = proj / "outro.txt"; g.write_text("y")
    assert cfg.resolve_or_register(g, home=home) == "Projetos/outro.txt"
    assert len(cfg.default_dirs) == 1


def test_resolve_arquivo_solto_no_home_vira_avulso(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    f = home / "solto.txt"
    f.write_text("x")
    cfg = ContentConfig.from_json(home / "dirs.json")

    vp = cfg.resolve_or_register(f, home=home)
    assert vp == "solto.txt"                              # item avulso, NÃO a home inteira
    assert cfg.default_dirs == []
    assert [Path(i).resolve() for i in cfg.extra_items] == [f.resolve()]


def test_resolve_fora_do_home_levanta(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    fora = tmp_path / "outro"; fora.mkdir()
    f = fora / "x.txt"; f.write_text("y")
    cfg = ContentConfig.from_json(home / "dirs.json")
    with pytest.raises(ValueError):
        cfg.resolve_or_register(f, home=home)
