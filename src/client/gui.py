"""GUI do cliente unuser — estilo Windows XP Explorer / Luna (PySide6 + QSS).

Visual da §5: painel de tarefas à esquerda com cabeçalhos em gradiente azul, árvore de
arquivos com as 6 cores de status (§7.1) e as ações da §7.2 na barra/menu de contexto.
O visual é homenagem estética — sem ícones/imagens proprietários.

A janela é desacoplada da rede: recebe um *controller* com os métodos
``status() -> list[FileState]``, ``send/receive/delete(paths)`` e ``connection_label()``.
Isso mantém a GUI testável sem servidor (ver ``tests/test_gui.py``). A fiação real
(config + VaultSession) é montada pelo subcomando ``unuser gui`` em :mod:`client.cli`.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFrame, QLabel, QMainWindow, QMenu, QMessageBox,
    QPushButton, QStatusBar, QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .sync import FileState, FileStatus

# As 6 cores de status (§7.1) — tons que combinam com o tema Luna.
STATUS_COLORS: dict[FileStatus, str] = {
    FileStatus.IN_SYNC: "#2e7d32",          # verde
    FileStatus.LOCAL_MODIFIED: "#e65100",   # laranja
    FileStatus.SERVER_MODIFIED: "#1565c0",  # azul
    FileStatus.CONFLICT: "#c62828",         # vermelho
    FileStatus.LOCAL_ONLY: "#00695c",       # teal
    FileStatus.SERVER_ONLY: "#6a1b9a",      # roxo
}

# Cromagem azul/verde do Luna: gradientes nas barras e cabeçalhos arredondados.
LUNA_QSS = """
QMainWindow, QWidget { background: #ece9d8; font-family: "Tahoma", "DejaVu Sans"; font-size: 11px; }
QToolBar { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #3a6ea5, stop:1 #1c3d6e);
           border-bottom: 1px solid #0a246a; spacing: 4px; padding: 3px; }
QToolBar QToolButton, QToolBar QLabel { color: white; padding: 3px 8px; }
QToolBar QToolButton:hover { background: #5a8fce; border-radius: 3px; }
QFrame#taskPane { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #6f9ee8, stop:1 #c5d9f1); }
QLabel.sectionHeader { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #4d80c8, stop:1 #2a5db0);
           color: white; font-weight: bold; padding: 4px 8px; border-top-left-radius: 6px;
           border-top-right-radius: 6px; }
QFrame.section { background: #d6e5f9; border: 1px solid #a6c0e3; border-radius: 6px; margin: 4px; }
QFrame.section QPushButton { background: transparent; border: none; color: #14387f; text-align: left;
           padding: 3px 10px; }
QFrame.section QPushButton:hover { color: #ec7a08; text-decoration: underline; }
QTreeWidget { background: white; border: 1px solid #7f9db9; }
QHeaderView::section { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #f4f3ee, stop:1 #d4d0c8);
           padding: 3px; border: 1px solid #aca899; }
QStatusBar { background: #ece9d8; }
"""

_ACTIONS = [
    ("atualizar", "Atualizar", "Compara local × servidor"),
    ("send", "Enviar →", "Substituir/versionar no servidor"),
    ("receive", "← Receber", "Substituir no local"),
    ("delete", "Apagar", "Tombstone no servidor + remove local"),
]


def _section(title: str, buttons: list[tuple[str, callable]]) -> QFrame:
    """Uma seção recolhível do painel de tarefas (cabeçalho azul + links)."""
    frame = QFrame()
    frame.setProperty("class", "section")
    frame.setObjectName("section")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(0, 0, 0, 6)
    lay.setSpacing(1)
    header = QLabel(title)
    header.setProperty("class", "sectionHeader")
    lay.addWidget(header)
    for label, slot in buttons:
        b = QPushButton(label)
        b.clicked.connect(slot)
        lay.addWidget(b)
    return frame


class MainWindow(QMainWindow):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.setWindowTitle("unuser — Cofre")
        self.resize(820, 520)
        self.setStyleSheet(LUNA_QSS)

        self._build_toolbar()
        self._build_body()
        self.setStatusBar(QStatusBar())
        self._set_status_text("pronto")

    # --- construção da UI ---------------------------------------------------

    def _build_toolbar(self) -> None:
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)
        self._actions: dict[str, QAction] = {}
        for key, label, tip in _ACTIONS:
            act = QAction(label, self)
            act.setToolTip(tip)
            act.triggered.connect(lambda _=False, k=key: self._run(k))
            tb.addAction(act)
            self._actions[key] = act
        tb.addSeparator()
        self.conn_label = QLabel(f"  {self._safe(self.controller.connection_label)}")
        tb.addWidget(self.conn_label)

    def _build_body(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)

        body = QWidget()
        from PySide6.QtWidgets import QHBoxLayout
        h = QHBoxLayout(body)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        # painel de tarefas (esquerda)
        pane = QFrame()
        pane.setObjectName("taskPane")
        pane.setFixedWidth(210)
        pv = QVBoxLayout(pane)
        pv.setContentsMargins(0, 6, 0, 6)
        pv.addWidget(_section("Tarefas de arquivo e pasta", [
            ("Enviar para o servidor", lambda: self._run("send")),
            ("Receber do servidor", lambda: self._run("receive")),
            ("Apagar", lambda: self._run("delete")),
        ]))
        pv.addWidget(_section("Outros locais", [
            ("Atualizar comparação", lambda: self._run("atualizar")),
        ]))
        self._details = QLabel("Detalhes:\nnenhum item selecionado")
        det = _section("Detalhes", [])
        det.layout().addWidget(self._details)
        pv.addWidget(det)
        pv.addStretch(1)
        h.addWidget(pane)

        # árvore de arquivos (direita)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Arquivo", "Status"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.itemSelectionChanged.connect(self._update_details)
        h.addWidget(self.tree, 1)

        root.addWidget(body)
        self.setCentralWidget(central)

    # --- dados --------------------------------------------------------------

    def set_states(self, states: list[FileState]) -> None:
        """Popula a árvore com os status, cada um na sua cor."""
        self.tree.clear()
        for st in states:
            item = QTreeWidgetItem([st.vault_path, st.status.value])
            color = QColor(STATUS_COLORS.get(st.status, "#000000"))
            item.setForeground(1, QBrush(color))
            item.setData(0, Qt.ItemDataRole.UserRole, st)
            self.tree.addTopLevelItem(item)
        self.tree.resizeColumnToContents(0)
        self._set_status_text(f"{len(states)} item(ns)")

    def selected_paths(self) -> list[str]:
        return [it.text(0) for it in self.tree.selectedItems()]

    # --- ações --------------------------------------------------------------

    def _run(self, key: str) -> None:
        try:
            if key == "atualizar":
                self.set_states(self.controller.status())
                return
            paths = self.selected_paths()
            if not paths:
                self._set_status_text("selecione ao menos um arquivo")
                return
            getattr(self.controller, key)(paths)
            self.set_states(self.controller.status())     # reflete o novo estado
            self._set_status_text(f"{key}: {len(paths)} item(ns)")
        except Exception as e:                              # erro de rede/cripto → diálogo
            QMessageBox.critical(self, "Erro", f"{type(e).__name__}: {e}")
            self._set_status_text("erro")

    def _context_menu(self, pos) -> None:
        if not self.tree.selectedItems():
            return
        menu = QMenu(self)
        for key, label, _tip in _ACTIONS[1:]:              # sem "atualizar" no contexto
            menu.addAction(label, lambda k=key: self._run(k))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _update_details(self) -> None:
        items = self.tree.selectedItems()
        if not items:
            self._details.setText("Detalhes:\nnenhum item selecionado")
            return
        st: FileState = items[0].data(0, Qt.ItemDataRole.UserRole)
        self._details.setText(f"Detalhes:\n{st.vault_path}\nstatus: {st.status.value}")

    # --- util ---------------------------------------------------------------

    def _set_status_text(self, msg: str) -> None:
        self.statusBar().showMessage(msg)

    @staticmethod
    def _safe(fn) -> str:
        try:
            return fn()
        except Exception:
            return "—"


def run(controller) -> int:
    """Sobe a aplicação Qt com a janela principal. Faz um Atualizar inicial."""
    app = QApplication.instance() or QApplication([])
    win = MainWindow(controller)
    try:
        win.set_states(controller.status())
    except Exception:
        pass                                               # deixa abrir mesmo offline
    win.show()
    return app.exec()
