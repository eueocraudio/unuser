"""Smoke test da GUI sem display (QT_QPA_PLATFORM=offscreen).

Valida que a janela constrói, popula a árvore com as 6 cores de status e dispara as
ações no controller — sem servidor nem display, via um controller falso.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # antes de criar a QApplication

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt                            # noqa: E402
from PySide6.QtWidgets import QApplication               # noqa: E402

from client.gui import STATUS_COLORS, MainWindow         # noqa: E402
from client.sync import FileState, FileStatus            # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class FakeController:
    def __init__(self, states):
        self._states = states
        self.calls: list[tuple[str, list[str]]] = []

    def status(self):
        return self._states

    def send(self, paths):
        self.calls.append(("send", paths))

    def receive(self, paths):
        self.calls.append(("receive", paths))

    def delete(self, paths):
        self.calls.append(("delete", paths))

    def add(self, local_paths):
        self.calls.append(("add", local_paths))

    def connection_label(self):
        return "Direto 127.0.0.1:8443"

    # --- gerência de pastas sincronizadas (para o SyncFoldersDialog) ---------
    def sync_folders(self):
        return {"dirs": list(getattr(self, "dirs", [])), "items": list(getattr(self, "items", []))}

    def add_root(self, path):
        self.dirs = getattr(self, "dirs", [])
        self.dirs.append((path, True))

    def remove_root(self, path):
        self.dirs = [(p, r) for p, r in getattr(self, "dirs", []) if p != path]

    def remove_item(self, path):
        self.items = [i for i in getattr(self, "items", []) if i != path]


def _all_status_states():
    return [FileState(f"Doc/{s.name.lower()}.txt", s, "h", "h", "h") for s in FileStatus]


def test_janela_constroi_e_popula_com_cores(app):
    ctrl = FakeController(_all_status_states())
    win = MainWindow(ctrl, async_run=False)
    win.set_states(ctrl.status())

    assert win.tree.topLevelItemCount() == len(FileStatus)
    # cada linha mostra o rótulo do status na cor certa
    for i in range(win.tree.topLevelItemCount()):
        item = win.tree.topLevelItem(i)
        st: FileState = item.data(0, Qt.ItemDataRole.UserRole)
        assert item.text(1) == st.status.value
        assert item.foreground(1).color().name() == STATUS_COLORS[st.status]


def _iter_folder_items(win):
    stack = [win.folder_tree.topLevelItem(0)]
    while stack:
        it = stack.pop()
        if it is None:
            continue
        yield it
        for i in range(it.childCount()):
            stack.append(it.child(i))


def _folder_paths(win):
    return {it.data(0, Qt.ItemDataRole.UserRole) for it in _iter_folder_items(win)}


def _select_folder(win, path):
    for it in _iter_folder_items(win):
        if it.data(0, Qt.ItemDataRole.UserRole) == path:
            win.folder_tree.setCurrentItem(it)           # dispara o filtro da lista
            return
    raise AssertionError(f"pasta não encontrada: {path!r}")


def _folder_item(win, path):
    for it in _iter_folder_items(win):
        if it.data(0, Qt.ItemDataRole.UserRole) == path:
            return it
    raise AssertionError(f"pasta não encontrada: {path!r}")


def test_treeview_preserva_expansao_e_selecao(app):
    states = [
        FileState("Documents/sub/b.txt", FileStatus.IN_SYNC, "h", "h", "h"),
        FileState("Fotos/c.png", FileStatus.SERVER_ONLY, None, None, "h"),
    ]
    win = MainWindow(FakeController(states), async_run=False)
    win.set_states(states)
    assert _folder_item(win, "Documents").isExpanded()   # 1ª montagem: tudo expandido

    # usuário colapsa "Documents" e seleciona "Fotos"
    _folder_item(win, "Documents").setExpanded(False)
    _select_folder(win, "Fotos")

    win.set_states(states)                                # rebuild (ex.: atualizar/ação)
    assert _folder_item(win, "Documents").isExpanded() is False   # colapso preservado
    assert win._current_folder() == "Fotos"              # seleção preservada

    # reexpande e reconstrói → continua expandido
    _folder_item(win, "Documents").setExpanded(True)
    win.set_states(states)
    assert _folder_item(win, "Documents").isExpanded() is True


def test_treeview_persiste_estado_entre_sessoes(app):
    """Fechar e reabrir o programa (novo MainWindow) deve restaurar expansão e seleção."""
    store: dict = {}

    class Persisted(FakeController):
        def ui_state(self):
            return dict(store)

        def save_ui_state(self, state):
            store.clear()
            store.update(state)

    states = [
        FileState("Documents/sub/b.txt", FileStatus.IN_SYNC, "h", "h", "h"),
        FileState("Fotos/c.png", FileStatus.SERVER_ONLY, None, None, "h"),
    ]
    win = MainWindow(Persisted(states), async_run=False)
    win.set_states(states)
    _folder_item(win, "Documents").setExpanded(False)     # colapsa → grava no store
    _select_folder(win, "Fotos")                          # seleciona → grava no store
    assert store.get("selected") == "Fotos"
    assert "Documents" not in store.get("expanded", [])

    # "reabrir o programa": novo MainWindow lê o mesmo store
    win2 = MainWindow(Persisted(states), async_run=False)
    win2.set_states(states)
    assert _folder_item(win2, "Documents").isExpanded() is False   # NÃO voltou expandido
    assert win2._current_folder() == "Fotos"


def test_arvore_de_pastas_filtra_a_lista(app):
    states = [
        FileState("Documents/a.txt", FileStatus.IN_SYNC, "h", "h", "h"),
        FileState("Documents/sub/b.txt", FileStatus.LOCAL_MODIFIED, "h2", "h", "h"),
        FileState("Fotos/c.png", FileStatus.SERVER_ONLY, None, None, "h"),
    ]
    win = MainWindow(FakeController(states), async_run=False)
    win.set_states(states)

    # a árvore de pastas tem a raiz ("") + todas as pastas e ancestrais
    assert _folder_paths(win) == {"", "Documents", "Documents/sub", "Fotos"}

    # NÃO recursivo: "Documents" mostra só os arquivos DIRETOS dela (a.txt), não sub/b.txt
    _select_folder(win, "Documents")
    rels = {win.tree.topLevelItem(i).text(0) for i in range(win.tree.topLevelItemCount())}
    assert rels == {"a.txt"}

    # a subpasta "Documents/sub" mostra o b.txt
    _select_folder(win, "Documents/sub")
    rels = {win.tree.topLevelItem(i).text(0) for i in range(win.tree.topLevelItemCount())}
    assert rels == {"b.txt"}

    # selecionar "Fotos" → só c.png (nome exibido é o basename; path absoluto preservado)
    _select_folder(win, "Fotos")
    assert win.tree.topLevelItemCount() == 1
    item = win.tree.topLevelItem(0)
    assert item.text(0) == "c.png"
    assert item.data(0, Qt.ItemDataRole.UserRole).vault_path == "Fotos/c.png"


def test_acoes_chamam_o_controller(app):
    ctrl = FakeController(_all_status_states())
    win = MainWindow(ctrl, async_run=False)
    win.set_states(ctrl.status())

    win.tree.selectAll()
    paths = win.selected_paths()
    assert len(paths) == len(FileStatus)

    win._run("send")
    assert ("send", paths) in ctrl.calls

    win.tree.selectAll()                               # ação repopula e limpa a seleção
    win._run("delete")
    assert ("delete", paths) in ctrl.calls


def test_adicionar_arquivo_chama_o_controller(app):
    ctrl = FakeController(_all_status_states())
    win = MainWindow(ctrl, async_run=False)

    win.add_files(["/home/user/Documents/novo.txt"])
    assert ("add", ["/home/user/Documents/novo.txt"]) in ctrl.calls
    # após adicionar, a lista é repopulada a partir do status()
    assert win.tree.topLevelItemCount() == len(FileStatus)

    win.add_files([])                                      # nada escolhido → no-op
    assert sum(1 for c in ctrl.calls if c[0] == "add") == 1


def test_dialogo_de_pastas_lista_e_atualiza(app):
    from client.gui import SyncFoldersDialog

    ctrl = FakeController(_all_status_states())
    ctrl.dirs = [("/home/user/Documents", True)]
    ctrl.items = ["/home/user/avulso.txt"]
    dlg = SyncFoldersDialog(ctrl)

    assert dlg.listw.count() == 2                          # 1 pasta + 1 avulso
    ctrl.add_root("/home/user/Projetos")
    dlg._reload()
    assert dlg.listw.count() == 3
    # a entrada nova carrega o tipo+caminho para o "Remover"
    kinds = {dlg.listw.item(i).data(Qt.ItemDataRole.UserRole)[0]
             for i in range(dlg.listw.count())}
    assert kinds == {"dir", "item"}


def test_itens_tem_icones_de_pasta_e_arquivo(app):
    states = [FileState("Documents/a.txt", FileStatus.IN_SYNC, "h", "h", "h")]
    win = MainWindow(FakeController(states), async_run=False)
    win.set_states(states)

    # pasta na árvore usa o ícone de diretório; raiz "Cofre" também
    assert _folder_item(win, "Documents").icon(0).cacheKey() == win._icon_dir.cacheKey()
    assert _folder_item(win, "").icon(0).cacheKey() == win._icon_dir.cacheKey()
    # arquivo na lista usa o ícone de arquivo
    _select_folder(win, "Documents")
    assert win.tree.topLevelItem(0).icon(0).cacheKey() == win._icon_file.cacheKey()


def test_atualizar_busca_do_controller(app):
    ctrl = FakeController(_all_status_states())
    win = MainWindow(ctrl, async_run=False)
    win.tree.clear()
    assert win.tree.topLevelItemCount() == 0
    win._run("atualizar")                               # deve repopular a partir do status()
    assert win.tree.topLevelItemCount() == len(FileStatus)


class SlowController(FakeController):
    """status() bloqueia até ser liberado — simula rede lenta (Tor)."""

    def __init__(self, states):
        super().__init__(states)
        import threading
        self.gate = threading.Event()

    def status(self):
        self.gate.wait(timeout=5)
        return self._states


def test_operacao_nao_bloqueia_a_ui(app):
    """Com async_run=True, _run retorna na hora (UI livre) e fica 'ocupado' até o worker
    terminar — provando que a chamada de rede não roda na thread da UI."""
    import time

    from PySide6.QtCore import QCoreApplication

    ctrl = SlowController(_all_status_states())
    win = MainWindow(ctrl, async_run=True)

    win._run("atualizar")                               # dispara no pool e retorna já
    assert win._busy is True                            # UI não ficou presa esperando

    ctrl.gate.set()                                     # libera o "trabalho lento"
    deadline = time.time() + 5
    while win._busy and time.time() < deadline:         # bombeia eventos até o sinal chegar
        QCoreApplication.processEvents()
        time.sleep(0.01)

    assert win._busy is False
    assert win.tree.topLevelItemCount() == len(FileStatus)
