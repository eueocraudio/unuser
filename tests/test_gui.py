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

    def send(self, paths, progress=None):
        self.calls.append(("send", paths))
        if progress:
            progress(len(paths), len(paths))

    def receive(self, paths, progress=None):
        self.calls.append(("receive", paths))

    def delete(self, paths, progress=None):
        self.calls.append(("delete", paths))

    def add(self, local_paths, progress=None):
        self.calls.append(("add", local_paths))

    def connection_label(self):
        return "Direto 127.0.0.1:8080"

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

    # --- servidor: uso de disco + export/import (para o ServerDialog) --------
    def server_usage(self):
        return {"disk_total": 8_000_000_000, "disk_used": 4_000_000_000,
                "disk_free": 4_000_000_000, "vault_bytes": 123_456, "blob_count": 7}

    def list_backups(self):
        return list(getattr(self, "backups", []))

    def export_server(self):
        self.calls.append(("export", []))
        self.backups = getattr(self, "backups", [])
        info = {"name": f"unuser-backup-2026010{len(self.backups)+1}-000000.tar",
                "size": 1024, "created": 1_700_000_000}
        self.backups.insert(0, info)
        return info

    def import_server(self, name):
        self.calls.append(("import", [name]))
        return 1


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


def test_lista_mostra_tamanho_data_e_ordena(app):
    """A lista tem colunas Tamanho/Data e ordena por valor (numérico/cronológico), não texto."""
    states = [
        FileState("Documents/grande.bin", FileStatus.IN_SYNC, "h", "h", "h",
                  size=3 * 1024 * 1024, mtime=1_700_000_000.0),
        FileState("Documents/pequeno.txt", FileStatus.LOCAL_MODIFIED, "h2", "h", "h",
                  size=512, mtime=1_600_000_000.0),
        FileState("Documents/medio.dat", FileStatus.IN_SYNC, "h", "h", "h",
                  size=20 * 1024, mtime=1_650_000_000.0),
    ]
    win = MainWindow(FakeController(states), async_run=False)
    win.set_states(states)
    _select_folder(win, "Documents")

    assert win.tree.columnCount() == 4
    assert [win.tree.headerItem().text(c) for c in range(4)] == \
        ["Arquivo", "Status", "Tamanho", "Data"]

    by_name = {win.tree.topLevelItem(i).text(0): win.tree.topLevelItem(i)
               for i in range(win.tree.topLevelItemCount())}
    assert by_name["pequeno.txt"].text(2) == "512 B"
    assert by_name["grande.bin"].text(2) == "3.0 MiB"
    assert by_name["pequeno.txt"].text(3)            # data formatada não-vazia

    def ordem():
        return [win.tree.topLevelItem(i).text(0) for i in range(win.tree.topLevelItemCount())]

    win.tree.sortByColumn(2, Qt.SortOrder.AscendingOrder)   # TAMANHO (numérico, não "20 KiB"<"512 B")
    assert ordem() == ["pequeno.txt", "medio.dat", "grande.bin"]
    win.tree.sortByColumn(3, Qt.SortOrder.AscendingOrder)   # DATA (cronológico)
    assert ordem() == ["pequeno.txt", "medio.dat", "grande.bin"]
    win.tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)   # NOME
    assert ordem() == ["grande.bin", "medio.dat", "pequeno.txt"]


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
    win._confirm_delete = lambda paths: True           # confirma o alerta (sem diálogo modal)
    win._run("delete")
    assert ("delete", paths) in ctrl.calls


def test_barra_de_progresso_no_rodape(app):
    """A barra de progresso do rodapé reflete o avanço por item da operação."""
    ctrl = FakeController(_all_status_states())
    win = MainWindow(ctrl, async_run=False)

    win._on_progress(3, 10)                              # N de M
    assert win._progress.maximum() == 10 and win._progress.value() == 3

    win.set_states(ctrl.status())
    win.tree.selectAll()
    n = win.tree.topLevelItemCount()
    win._run("send")                                    # FakeController.send reporta n/n
    assert win._progress.maximum() == n and win._progress.value() == n


def test_apagar_pede_confirmacao_e_cancela(app):
    """Apagar só chama o controller se a confirmação for aceita; recusar não apaga nada."""
    ctrl = FakeController(_all_status_states())
    win = MainWindow(ctrl, async_run=False)
    win.set_states(ctrl.status())
    win.tree.selectAll()

    win._confirm_delete = lambda paths: False          # usuário recusa o alerta
    win._run("delete")
    assert not any(c[0] == "delete" for c in ctrl.calls)   # nada apagado

    win._confirm_delete = lambda paths: True           # usuário confirma
    win.tree.selectAll()
    win._run("delete")
    assert any(c[0] == "delete" for c in ctrl.calls)       # agora apagou


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


def test_barra_de_uso_de_disco_do_servidor(app):
    """Atualizar busca o uso de disco do servidor e preenche a barra do rodapé."""
    ctrl = FakeController(_all_status_states())
    win = MainWindow(ctrl, async_run=False)
    win._run("atualizar")                                # _collect() chama server_usage()
    assert win._disk_bar.isHidden() is False            # janela não é .show()n: usa isHidden
    assert win._disk_bar.value() == 50                  # 4 GB de 8 GB
    assert "de" in win._disk_label.text()

    win.set_server_usage(None)                          # sem dado → oculta
    assert win._disk_bar.isHidden() is True


def test_uso_de_disco_e_best_effort(app):
    """Se server_usage() falha, a ação não quebra (uso é informativo)."""
    ctrl = FakeController(_all_status_states())

    def boom():
        raise RuntimeError("servidor fora do ar")
    ctrl.server_usage = boom

    win = MainWindow(ctrl, async_run=False)
    win._run("atualizar")
    assert win.tree.topLevelItemCount() == len(FileStatus)   # status seguiu normal
    assert win._disk_bar.isHidden() is True                  # sem barra de uso


def test_server_dialog_exporta_e_restaura(app, monkeypatch):
    """ServerDialog mostra o uso, exporta um backup e restaura o selecionado (com confirmação)."""
    import client.gui as gui_mod
    from client.gui import ServerDialog

    # neutraliza os diálogos modais (information/question) p/ não travar sem display
    monkeypatch.setattr(gui_mod.QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(gui_mod.QMessageBox, "question",
                        staticmethod(lambda *a, **k: gui_mod.QMessageBox.StandardButton.Yes))

    ctrl = FakeController(_all_status_states())
    dlg = ServerDialog(ctrl)
    assert dlg.disk_bar.value() == 50                   # uso preenchido na abertura
    assert dlg.listw.count() == 0                       # ainda sem backups

    dlg._export()                                       # cria um backup
    assert any(c[0] == "export" for c in ctrl.calls)
    assert dlg.listw.count() == 1                       # lista recarregada

    dlg.listw.setCurrentRow(0)
    name = dlg.listw.currentItem().data(Qt.ItemDataRole.UserRole)
    dlg._restore()                                      # confirma (question→Yes) e restaura
    assert ("import", [name]) in ctrl.calls


def test_server_dialog_restaurar_recusado_nao_importa(app, monkeypatch):
    import client.gui as gui_mod
    from client.gui import ServerDialog

    monkeypatch.setattr(gui_mod.QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(gui_mod.QMessageBox, "question",
                        staticmethod(lambda *a, **k: gui_mod.QMessageBox.StandardButton.No))

    ctrl = FakeController(_all_status_states())
    dlg = ServerDialog(ctrl)
    dlg._export()
    dlg.listw.setCurrentRow(0)
    dlg._restore()                                      # usuário recusa → nada importado
    assert not any(c[0] == "import" for c in ctrl.calls)


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
