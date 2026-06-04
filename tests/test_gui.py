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


def test_arvore_de_pastas_filtra_a_lista(app):
    states = [
        FileState("Documents/a.txt", FileStatus.IN_SYNC, "h", "h", "h"),
        FileState("Documents/sub/b.txt", FileStatus.LOCAL_MODIFIED, "h2", "h", "h"),
        FileState("Fotos/c.png", FileStatus.SERVER_ONLY, None, None, "h"),
    ]
    win = MainWindow(FakeController(states), async_run=False)
    win.set_states(states)

    # raiz "Cofre" selecionada por padrão → lista mostra TODOS os arquivos
    assert win.tree.topLevelItemCount() == 3
    # a árvore de pastas tem a raiz ("") + todas as pastas e ancestrais
    assert _folder_paths(win) == {"", "Documents", "Documents/sub", "Fotos"}

    # selecionar "Documents" filtra a lista para os arquivos sob ela (relativos à pasta)
    _select_folder(win, "Documents")
    rels = {win.tree.topLevelItem(i).text(0) for i in range(win.tree.topLevelItemCount())}
    assert rels == {"a.txt", "sub/b.txt"}

    # selecionar "Fotos" → só c.png; o path absoluto fica preservado no FileState
    _select_folder(win, "Fotos")
    assert win.tree.topLevelItemCount() == 1
    st = win.tree.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole)
    assert st.vault_path == "Fotos/c.png"


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
