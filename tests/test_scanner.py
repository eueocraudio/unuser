"""Testes da varredura local e das regras de exclusão (.unuserignore)."""

from client import scanner
from client.scanner import IgnoreRules


def _touch(p, data=b"x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


# --- regras de ignore -------------------------------------------------------

def test_defaults_de_seguranca_excluem_segredos():
    ig = IgnoreRules.load()  # só os defaults embutidos
    assert ig.matches(".env")
    assert ig.matches("id_rsa.key")
    assert ig.matches("cert.pem")
    assert ig.matches("sub/dir/chave.key")          # glob no nome-base em qualquer nível
    assert not ig.matches("relatorio.txt")


def test_padrao_de_diretorio_exclui_em_qualquer_nivel():
    ig = IgnoreRules.load()
    assert ig.matches(".git/config")                # ancestral .git/ excluído
    assert ig.matches("proj/.git/HEAD")
    assert ig.matches(".ssh/known_hosts")


def test_unuserignore_do_usuario_soma_aos_defaults(tmp_path):
    (tmp_path / ".unuserignore").write_text("# meus\n*.log\nbuild/\n")
    ig = IgnoreRules.load(tmp_path / ".unuserignore")
    assert ig.matches("app.log")                    # do usuário
    assert ig.matches("build/out.bin")              # do usuário (diretório)
    assert ig.matches(".env")                       # default ainda vale


# --- varredura --------------------------------------------------------------

def test_scan_prefixa_pelo_nome_da_raiz_e_pula_ignorados(tmp_path):
    root = tmp_path / "Documentos"
    _touch(root / "relatorio.txt", b"conteudo")
    _touch(root / "sub" / "nota.md", b"nota")
    _touch(root / "segredo.key", b"nao-deve-entrar")
    _touch(root / ".git" / "HEAD", b"ref")

    found = scanner.scan([(root, True)])

    assert set(found) == {"Documentos/relatorio.txt", "Documentos/sub/nota.md"}
    sf = found["Documentos/relatorio.txt"]
    assert sf.hash.startswith("blake3:") and sf.size == 8


def test_scan_nao_recursivo_ignora_subpastas(tmp_path):
    root = tmp_path / "Projetos"
    _touch(root / "topo.txt")
    _touch(root / "sub" / "fundo.txt")
    found = scanner.scan([(root, False)])
    assert set(found) == {"Projetos/topo.txt"}


def test_extra_items_entram_pelo_basename(tmp_path):
    item = tmp_path / "notas" / "ideias.md"
    _touch(item, b"ideia")
    found = scanner.scan([], extra_items=[item])
    assert set(found) == {"ideias.md"}
