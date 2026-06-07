"""GUI do cliente unuser — Explorer de duas áreas em tema escuro (PySide6 + QSS).

Layout estilo Explorer (§5): à **esquerda** uma árvore de **pastas** (TreeView
hierárquica, com a raiz "Cofre"); à **direita** a **lista** dos arquivos **diretamente**
na pasta selecionada (não recursivo — subpastas ficam na árvore), cada um com a sua cor
de status (§7.1). As ações da §7.2 ficam na barra de ferramentas e no menu de contexto da
lista. Paleta escura; sem ícones/imagens proprietários.

A janela é desacoplada da rede: recebe um *controller* com os métodos
``status() -> list[FileState]``, ``send/receive/delete(paths)`` e ``connection_label()``.
Isso mantém a GUI testável sem servidor (ver ``tests/test_gui.py``). A fiação real
(config + VaultSession) é montada pelo subcomando ``unuser gui`` em :mod:`client.cli`.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QAction, QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QSplitter, QStatusBar,
    QStyle, QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .sync import FileState, FileStatus

_ROLE = Qt.ItemDataRole.UserRole
_SORT_ROLE = Qt.ItemDataRole.UserRole + 1    # chave de ordenação por coluna (num/cronológica)


def _human_size(n: int | None) -> str:
    """Tamanho legível (1.2 MiB). Vazio quando desconhecido."""
    if n is None:
        return ""
    f = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if f < 1024 or unit == "TiB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return ""


def _fmt_date(epoch: float | None) -> str:
    """Data de modificação local (YYYY-MM-DD HH:MM). Vazio quando desconhecida."""
    if not epoch:
        return ""
    import datetime
    return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


class _FileItem(QTreeWidgetItem):
    """Item da lista de arquivos: ordena pela CHAVE em ``_SORT_ROLE`` (não pelo texto),
    para tamanho ordenar numericamente e data cronologicamente."""

    def __lt__(self, other) -> bool:
        tw = self.treeWidget()
        col = tw.sortColumn() if tw is not None else 0
        a, b = self.data(col, _SORT_ROLE), other.data(col, _SORT_ROLE)
        if a is None or b is None:
            return super().__lt__(other)
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)

# As 6 cores de status (§7.1) — tons vivos, legíveis sobre o fundo escuro.
STATUS_COLORS: dict[FileStatus, str] = {
    FileStatus.IN_SYNC: "#66bb6a",          # verde
    FileStatus.LOCAL_MODIFIED: "#ffa726",   # laranja
    FileStatus.SERVER_MODIFIED: "#42a5f5",  # azul
    FileStatus.CONFLICT: "#ef5350",         # vermelho
    FileStatus.LOCAL_ONLY: "#26c6da",       # teal
    FileStatus.SERVER_ONLY: "#ab47bc",      # roxo
}

# Tema escuro: fundos grafite, texto claro, acentos em azul.
DARK_QSS = """
QMainWindow, QWidget { background: #232629; color: #d6d6d6; font-family: "Tahoma", "DejaVu Sans"; font-size: 11px; }
QToolBar { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #3a3d42, stop:1 #26282c);
           border-bottom: 1px solid #15171a; spacing: 4px; padding: 3px; }
QToolBar QToolButton, QToolBar QLabel { color: #eaeaea; padding: 3px 8px; }
QToolBar QToolButton:hover { background: #3f5e8c; border-radius: 3px; }
QToolBar QToolButton:disabled { color: #7a7d82; }
QTreeWidget { background: #1b1d20; alternate-background-color: #232629; color: #d6d6d6;
           border: 1px solid #3c4046; outline: 0; }
QTreeWidget::item { padding: 1px 0; }
QTreeWidget::item:selected { background: #094771; color: #ffffff; }
QTreeWidget::branch:selected { background: #094771; }
QHeaderView::section { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #33363b, stop:1 #2a2c30);
           color: #d6d6d6; padding: 3px; border: 1px solid #45494f; }
QSplitter::handle { background: #2a2d31; }
QMenu { background: #2a2d31; color: #d6d6d6; border: 1px solid #45494f; }
QMenu::item:selected { background: #094771; color: #ffffff; }
QStatusBar { background: #26282c; color: #cfd2d6; }
"""

_ACTIONS = [
    ("atualizar", "Atualizar", "Compara local × servidor"),
    ("send", "Enviar →", "Substituir/versionar no servidor"),
    ("receive", "← Receber", "Substituir no local"),
    ("delete", "Apagar", "Tombstone no servidor + remove local"),
]


class _WorkerSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Worker(QRunnable):
    """Roda ``fn()`` numa thread do pool e devolve o resultado/erro por sinal (entregue
    na thread da UI). É como a GUI não trava enquanto a rede (Tor) responde."""

    def __init__(self, fn):
        super().__init__()
        self._fn = fn
        self.signals = _WorkerSignals()

    def run(self) -> None:
        try:
            self.signals.done.emit(self._fn())
        except Exception as e:                          # qualquer erro vai para a UI
            self.signals.failed.emit(f"{type(e).__name__}: {e}")


class SyncFoldersDialog(QDialog):
    """Tela de gerência das **pastas sincronizadas** (raízes do cofre) + itens avulsos.

    Lista o que está em ``dirs.json`` e permite adicionar uma pasta (seletor de
    diretório) ou remover a seleção — tudo via ``controller`` (``sync_folders()``,
    ``add_root``/``remove_root``/``remove_item``)."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Pastas sincronizadas")
        self.setStyleSheet(DARK_QSS)
        self.resize(480, 340)
        style = self.style()
        self._icon_dir = style.standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        self._icon_file = style.standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Pastas (e arquivos avulsos) que entram no cofre:"))
        self.listw = QListWidget()
        self.listw.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        lay.addWidget(self.listw, 1)
        row = QHBoxLayout()
        add_b = QPushButton("Adicionar pasta…")
        add_b.clicked.connect(self._pick_and_add)
        rem_b = QPushButton("Remover")
        rem_b.clicked.connect(self._remove_selected)
        close_b = QPushButton("Fechar")
        close_b.clicked.connect(self.accept)
        row.addWidget(add_b)
        row.addWidget(rem_b)
        row.addStretch(1)
        row.addWidget(close_b)
        lay.addLayout(row)
        self._reload()

    def _reload(self) -> None:
        self.listw.clear()
        data = self.controller.sync_folders()
        for path, rec in data.get("dirs", []):
            suffix = "recursivo" if rec else "não recursivo"
            it = QListWidgetItem(self._icon_dir, f"{path}   ({suffix})")
            it.setData(Qt.ItemDataRole.UserRole, ("dir", path))
            self.listw.addItem(it)
        for item in data.get("items", []):
            it = QListWidgetItem(self._icon_file, f"{item}   (arquivo avulso)")
            it.setData(Qt.ItemDataRole.UserRole, ("item", item))
            self.listw.addItem(it)

    def _pick_and_add(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        d = QFileDialog.getExistingDirectory(self, "Escolher pasta para sincronizar")
        if d:
            self.controller.add_root(d)
            self._reload()

    def _remove_selected(self) -> None:
        it = self.listw.currentItem()
        if it is None:
            return
        kind, path = it.data(Qt.ItemDataRole.UserRole)
        (self.controller.remove_root if kind == "dir" else self.controller.remove_item)(path)
        self._reload()


class MainWindow(QMainWindow):
    def __init__(self, controller, *, async_run: bool = True):
        super().__init__()
        self.controller = controller
        self._async = async_run                          # False nos testes (determinístico)
        self._busy = False
        self._states: list[FileState] = []               # último status conhecido
        self._expanded, self._selected = self._load_ui_state()  # estado da árvore (persistido)
        self._workers: set = set()                       # mantém refs vivas até concluírem
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)                  # serializa: 1 operação por vez
        self.setWindowTitle("unuser — Cofre")
        self.resize(820, 520)
        self.setStyleSheet(DARK_QSS)

        style = self.style()                             # ícones padrão do Qt (sem assets próprios)
        self._icon_dir = style.standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        self._icon_file = style.standardIcon(QStyle.StandardPixmap.SP_FileIcon)

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
        add_act = QAction("Adicionar arquivo", self)
        add_act.setToolTip("Escolher um arquivo e enviá-lo ao cofre (registra a pasta se preciso)")
        add_act.triggered.connect(self._pick_and_add)
        tb.addAction(add_act)
        self._actions["add"] = add_act
        folders_act = QAction("Pastas…", self)
        folders_act.setToolTip("Gerenciar as pastas sincronizadas (raízes do cofre)")
        folders_act.triggered.connect(self._open_folders_dialog)
        tb.addAction(folders_act)
        self._actions["folders"] = folders_act
        tb.addSeparator()
        self.conn_label = QLabel(f"  {self._safe(self.controller.connection_label)}")
        tb.addWidget(self.conn_label)

    def _build_body(self) -> None:
        split = QSplitter(Qt.Orientation.Horizontal)

        # esquerda: árvore de PASTAS
        self.folder_tree = QTreeWidget()
        self.folder_tree.setHeaderLabel("Pastas")
        self.folder_tree.setColumnCount(1)
        self.folder_tree.itemSelectionChanged.connect(self._on_folder_selected)
        self.folder_tree.itemExpanded.connect(self._remember_tree_state)
        self.folder_tree.itemCollapsed.connect(self._remember_tree_state)
        split.addWidget(self.folder_tree)

        # direita: LISTA de arquivos da pasta selecionada
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Arquivo", "Status", "Tamanho", "Data"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSortingEnabled(True)                 # clicar no cabeçalho ordena
        self.tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)   # padrão: por nome
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.itemSelectionChanged.connect(self._update_details)
        split.addWidget(self.tree)

        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([220, 600])

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(split)
        self.setCentralWidget(central)

    # --- dados / navegação --------------------------------------------------

    def set_states(self, states: list[FileState]) -> None:
        """Recebe o status do cofre, monta a árvore de pastas e mostra a pasta atual."""
        self._states = list(states)
        self._rebuild_folder_tree()
        self._set_status_text(f"{len(self._states)} item(ns)")

    def _rebuild_folder_tree(self) -> None:
        """(Re)constrói a árvore preservando expansão/seleção — inclusive ENTRE sessões.

        O estado (pastas expandidas + pasta selecionada) é lido/gravado via
        ``controller.ui_state``/``save_ui_state`` (persistência em disco). Sem estado salvo
        (1ª execução), a árvore abre toda expandida; pastas novas nascem colapsadas.
        """
        if self.folder_tree.topLevelItemCount():         # refresca com o estado vivo da sessão
            self._expanded = self._expanded_folders()

        self.folder_tree.blockSignals(True)              # não dispara seleção/expansão no rebuild
        self.folder_tree.clear()
        root = QTreeWidgetItem(["Cofre"])
        root.setData(0, _ROLE, "")                       # "" = raiz
        root.setIcon(0, self._icon_dir)
        self.folder_tree.addTopLevelItem(root)
        nodes: dict[str, QTreeWidgetItem] = {"": root}
        for folder in sorted(self._folders()):
            parts = folder.split("/")
            parent = nodes.get("/".join(parts[:-1]), root)
            item = QTreeWidgetItem([parts[-1]])
            item.setData(0, _ROLE, folder)
            item.setIcon(0, self._icon_dir)
            parent.addChild(item)
            nodes[folder] = item
        for path, item in nodes.items():                 # None = 1ª vez (sem estado): tudo aberto
            item.setExpanded(True if self._expanded is None else path in self._expanded)
        keep = nodes.get(self._selected) if self._selected is not None else None
        target = keep or self._default_node(root)        # mantém a pasta; senão, padrão
        self._selected = target.data(0, _ROLE) or ""
        self.folder_tree.setCurrentItem(target)
        self.folder_tree.blockSignals(False)

        if self._expanded is None:                       # carimba o estado inicial (tudo aberto)
            self._expanded = self._expanded_folders()
        self._save_ui_state()
        self._show_folder(self._selected)                # popula a lista da pasta atual

    def _expanded_folders(self) -> set[str]:
        """Conjunto das pastas atualmente expandidas na árvore (por vault path)."""
        out: set[str] = set()
        stack = [self.folder_tree.topLevelItem(i)
                 for i in range(self.folder_tree.topLevelItemCount())]
        while stack:
            it = stack.pop()
            if it is None:
                continue
            if it.isExpanded():
                out.add(it.data(0, _ROLE) or "")
            for i in range(it.childCount()):
                stack.append(it.child(i))
        return out

    def _remember_tree_state(self, *_args) -> None:
        """Salva o estado quando o usuário expande/colapsa um nó."""
        self._expanded = self._expanded_folders()
        self._save_ui_state()

    def _load_ui_state(self):
        """Lê o estado persistido da árvore: (expanded:set|None, selected:str|None)."""
        st = {}
        getter = getattr(self.controller, "ui_state", None)
        if getter is not None:
            try:
                st = getter() or {}
            except Exception:
                st = {}
        exp = st.get("expanded")
        return (set(exp) if exp is not None else None), st.get("selected")

    def _save_ui_state(self) -> None:
        saver = getattr(self.controller, "save_ui_state", None)
        if saver is None:
            return
        try:
            saver({"expanded": sorted(self._expanded or []), "selected": self._selected})
        except Exception:
            pass                                         # persistência é best-effort

    def _default_node(self, root: QTreeWidgetItem) -> QTreeWidgetItem:
        """Pasta a selecionar ao (re)abrir: a raiz se ela tem arquivos soltos; senão a
        primeira subpasta (para não abrir numa lista vazia); senão a própria raiz."""
        if any("/" not in st.vault_path for st in self._states):
            return root
        return root.child(0) if root.childCount() else root

    def _folders(self) -> set[str]:
        """Conjunto de todas as pastas (e ancestrais) presentes nos caminhos atuais."""
        out: set[str] = set()
        for st in self._states:
            parts = st.vault_path.split("/")[:-1]        # tira o nome do arquivo
            for i in range(1, len(parts) + 1):
                out.add("/".join(parts[:i]))
        return out

    def _current_folder(self) -> str:
        it = self.folder_tree.currentItem()
        return "" if it is None else (it.data(0, _ROLE) or "")

    def _on_folder_selected(self) -> None:
        self._selected = self._current_folder()
        self._save_ui_state()
        self._show_folder(self._selected)

    def _show_folder(self, folder: str) -> None:
        """Popula a lista da direita SÓ com os arquivos **diretamente** em ``folder`` (não
        recursivo — subpastas ficam na árvore à esquerda). Cada arquivo na sua cor."""
        self.tree.setSortingEnabled(False)               # popular e ordenar 1x no fim (rápido)
        self.tree.clear()
        for st in self._states:
            head, sep, name = st.vault_path.rpartition("/")
            parent = head if sep else ""                 # pasta-pai do arquivo
            if parent != folder:
                continue
            item = _FileItem([name, st.status.value, _human_size(st.size), _fmt_date(st.mtime)])
            item.setIcon(0, self._icon_file)
            item.setForeground(1, QBrush(QColor(STATUS_COLORS.get(st.status, "#d6d6d6"))))
            item.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item.setData(0, _ROLE, st)                   # FileState completo (path absoluto)
            # chaves de ordenação: nome (texto), status (texto), tamanho (int), data (epoch)
            item.setData(0, _SORT_ROLE, name.lower())
            item.setData(1, _SORT_ROLE, st.status.value)
            item.setData(2, _SORT_ROLE, st.size if st.size is not None else -1)
            item.setData(3, _SORT_ROLE, st.mtime if st.mtime is not None else -1.0)
            self.tree.addTopLevelItem(item)
        self.tree.setSortingEnabled(True)                 # re-aplica a ordenação corrente
        for col in (0, 2, 3):
            self.tree.resizeColumnToContents(col)
        self._update_details()

    def selected_paths(self) -> list[str]:
        """Caminhos de cofre dos arquivos selecionados na lista da direita."""
        out: list[str] = []
        for it in self.tree.selectedItems():
            st: FileState | None = it.data(0, _ROLE)
            if st is not None:
                out.append(st.vault_path)
        return out

    # --- ações --------------------------------------------------------------

    def _run(self, key: str) -> None:
        if self._busy:                                     # uma operação por vez
            return
        if key == "atualizar":
            self._submit(self.controller.status, self.set_states, "atualizado")
            return
        paths = self.selected_paths()
        if not paths:
            self._set_status_text("selecione ao menos um arquivo")
            return

        def work():
            getattr(self.controller, key)(paths)           # ação (pode demorar via Tor)
            return self.controller.status()                # já devolve o novo estado

        self._submit(work, self.set_states, f"{key}: {len(paths)} item(ns)")

    def _pick_and_add(self) -> None:
        """Abre um seletor de arquivos e adiciona os escolhidos ao cofre."""
        if self._busy:
            return
        from PySide6.QtWidgets import QFileDialog
        start = self._safe(getattr(self.controller, "add_start_dir", lambda: ""))
        files, _ = QFileDialog.getOpenFileNames(self, "Adicionar arquivo ao cofre", start)
        self.add_files(files)

    def add_files(self, local_paths: list[str]) -> None:
        """Envia ao cofre os arquivos locais escolhidos (via ``controller.add``)."""
        if self._busy or not local_paths:
            return

        def work():
            self.controller.add(local_paths)               # mapeia p/ vault path e envia
            return self.controller.status()

        self._submit(work, self.set_states, f"adicionado(s) {len(local_paths)} arquivo(s)")

    def _open_folders_dialog(self) -> None:
        """Abre a tela de pastas sincronizadas; ao fechar, atualiza a comparação."""
        if self._busy:
            return
        SyncFoldersDialog(self.controller, self).exec()
        self._run("atualizar")                             # pastas podem ter mudado

    def _submit(self, fn, on_done, done_msg: str) -> None:
        """Executa ``fn`` fora da thread da UI e entrega o resultado a ``on_done`` na UI."""
        self._set_busy(True)

        def deliver(result):
            on_done(result)
            self._set_busy(False)
            self._set_status_text(done_msg)

        if not self._async:                                # caminho síncrono (testes)
            try:
                deliver(fn())
            except Exception as e:
                self._fail(f"{type(e).__name__}: {e}")
            return
        worker = _Worker(fn)
        worker.setAutoDelete(False)                        # nós controlamos o tempo de vida
        self._workers.add(worker)                          # senão é coletado antes do sinal
        worker.signals.done.connect(deliver)
        worker.signals.failed.connect(self._fail)
        worker.signals.done.connect(lambda *_: self._workers.discard(worker))
        worker.signals.failed.connect(lambda *_: self._workers.discard(worker))
        self._pool.start(worker)

    def _fail(self, msg: str) -> None:
        self._set_busy(False)
        self._set_status_text("erro")
        QMessageBox.critical(self, "Erro", msg)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for act in self._actions.values():
            act.setEnabled(not busy)
        self.setCursor(Qt.CursorShape.WaitCursor if busy else Qt.CursorShape.ArrowCursor)
        if busy:
            self._set_status_text("trabalhando…")

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
            self._set_status_text(f"{self.tree.topLevelItemCount()} item(ns)")
            return
        if len(items) == 1:
            st: FileState = items[0].data(0, _ROLE)
            self._set_status_text(f"{st.vault_path} — {st.status.value}")
        else:
            self._set_status_text(f"{len(items)} selecionados")

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
    """Sobe a aplicação Qt com a janela principal e dispara a comparação inicial."""
    app = QApplication.instance() or QApplication([])
    win = MainWindow(controller)
    win.show()
    win._run("atualizar")                                  # assíncrono: não trava a abertura
    return app.exec()
