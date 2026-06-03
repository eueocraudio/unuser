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

    def connection_label(self):
        return "Direto 127.0.0.1:8443"


def _all_status_states():
    return [FileState(f"Doc/{s.name.lower()}.txt", s, "h", "h", "h") for s in FileStatus]


def test_janela_constroi_e_popula_com_cores(app):
    ctrl = FakeController(_all_status_states())
    win = MainWindow(ctrl)
    win.set_states(ctrl.status())

    assert win.tree.topLevelItemCount() == len(FileStatus)
    # cada linha mostra o rótulo do status na cor certa
    for i in range(win.tree.topLevelItemCount()):
        item = win.tree.topLevelItem(i)
        st: FileState = item.data(0, Qt.ItemDataRole.UserRole)
        assert item.text(1) == st.status.value
        assert item.foreground(1).color().name() == STATUS_COLORS[st.status]


def test_acoes_chamam_o_controller(app):
    ctrl = FakeController(_all_status_states())
    win = MainWindow(ctrl)
    win.set_states(ctrl.status())

    win.tree.selectAll()
    paths = win.selected_paths()
    assert len(paths) == len(FileStatus)

    win._run("send")
    assert ("send", paths) in ctrl.calls

    win.tree.selectAll()                               # ação repopula e limpa a seleção
    win._run("delete")
    assert ("delete", paths) in ctrl.calls


def test_atualizar_busca_do_controller(app):
    ctrl = FakeController(_all_status_states())
    win = MainWindow(ctrl)
    win.tree.clear()
    assert win.tree.topLevelItemCount() == 0
    win._run("atualizar")                               # deve repopular a partir do status()
    assert win.tree.topLevelItemCount() == len(FileStatus)
