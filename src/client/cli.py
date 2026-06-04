"""CLI mínimo do cliente unuser — torna o motor de sync executável de ponta a ponta.

Subcomandos: ``status``, ``send``, ``receive``, ``delete``. Lê a config da §9
(``~/.env`` + ``~/.config/unuser/dirs.json`` + ``~/.unuserignore``), destrava o cofre
(passphrase + keyfile) e aplica as ações de :mod:`client.actions`.

A passphrase vem do prompt (``getpass``) ou da variável ``UNUSER_PASSPHRASE`` (útil para
automação/testes). O keyfile é um arquivo apontado por ``--keyfile``/``UNUSER_KEYFILE``.
O *salt* do Argon2 (não-secreto) é guardado localmente no índice; um cofre existente numa
máquina nova precisa do salt copiado junto (limitação conhecida do MVP).
"""

from __future__ import annotations

import argparse
import base64
import getpass
import os
import sys
from pathlib import Path

from cryptography.exceptions import InvalidTag

from . import config, crypto, manifest, scanner
from .actions import NothingToReceive, VaultSession
from .index import Index
from .sync import FileStatus, SyncEngine

_HOME = Path.home()
DEFAULTS = {
    "env": _HOME / ".env",
    "dirs": _HOME / ".config" / "unuser" / "dirs.json",
    "ignore": _HOME / ".unuserignore",
    "index": _HOME / ".config" / "unuser" / "index.db",
}
# Dados temporários/descartáveis (ex.: estado da GUI). Criado recursivamente ao gravar.
_DATA_DIR = _HOME / ".local" / "data" / "unuser"
_LOCAL_CHANGED = {FileStatus.LOCAL_ONLY, FileStatus.LOCAL_MODIFIED}


def _die(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    raise SystemExit(f"unuser: {msg}")


def _local_or_new_salt(index: Index) -> bytes:
    """Salt para um cofre NOVO: usa o cache local ou gera e persiste."""
    raw = index.get_meta("argon2_salt")
    if raw:
        return base64.b64decode(raw)
    salt = crypto.generate_salt()
    index.set_meta("argon2_salt", base64.b64encode(salt).decode())
    return salt


def _unlock(client, index: Index, keyfile_path) -> tuple[crypto.Keyring, bytes]:
    if not keyfile_path:
        _die("defina o keyfile com --keyfile ou UNUSER_KEYFILE")
    keyfile = Path(keyfile_path).read_bytes()
    passphrase = os.environ.get("UNUSER_PASSPHRASE") or getpass.getpass("Passphrase: ")
    _ver, wire = client.get_manifest()
    if wire:
        salt = manifest.peek_salt(wire)               # do servidor, EM CLARO (máquina nova)
        index.set_meta("argon2_salt", base64.b64encode(salt).decode())  # cacheia
    else:
        salt = _local_or_new_salt(index)              # cofre novo
    kr = crypto.unlock(passphrase, keyfile, salt)
    if wire:                                           # valida cedo: chave abre o manifesto?
        try:
            manifest.unpack(wire, kr.manifest_key)
        except InvalidTag:
            _die("passphrase/keyfile incorretos (ou salt divergente)")
    return kr, salt


def _build(args, *, same_thread: bool = True) -> tuple[config.ContentConfig, Index, VaultSession]:
    conn = config.ConnConfig.from_env(args.env)
    content = config.ContentConfig.from_json(args.dirs)
    Path(args.index).parent.mkdir(parents=True, exist_ok=True)
    index = Index(args.index, check_same_thread=same_thread)
    client = conn.make_client()
    kr, salt = _unlock(client, index, args.keyfile or os.environ.get("UNUSER_KEYFILE"))
    session = VaultSession(
        client=client, keyring=kr, index=index, resolver=content.path_resolver(),
        device=args.device, vault_id=conn.vault_id, salt=salt,
    )
    return content, index, session


# --- subcomandos ------------------------------------------------------------

def cmd_status(args) -> int:
    content, index, session = _build(args)
    scanned = content.scan(ignore_path=args.ignore)
    _ver, vault = session._load()
    for fs in SyncEngine(index).status(scanned, vault):
        print(f"{fs.status.value:24} {fs.vault_path}")
    return 0


def cmd_send(args) -> int:
    content, index, session = _build(args)
    scanned = content.scan(ignore_path=args.ignore)
    if args.paths:
        alvos = [scanned[p] for p in args.paths if p in scanned]
    else:  # sem alvos: envia tudo que mudou localmente
        mudados = {fs.vault_path for fs in SyncEngine(index).status(scanned, session._load()[1])
                   if fs.status in _LOCAL_CHANGED}
        alvos = [sf for vp, sf in scanned.items() if vp in mudados]
    if not alvos:
        print("nada a enviar.")
        return 0
    session.send_many(alvos)
    print(f"enviados {len(alvos)} arquivo(s).")
    return 0


def cmd_receive(args) -> int:
    _content, _index, session = _build(args)
    for vp in args.paths:
        try:
            dest = session.receive(vp)
            print(f"recebido: {vp} -> {dest}")
        except NothingToReceive:
            print(f"nada a receber (inexistente ou apagado no servidor): {vp}")
    return 0


def cmd_delete(args) -> int:
    _content, _index, session = _build(args)
    for vp in args.paths:
        session.delete(vp)
        print(f"apagado: {vp}")
    return 0


def cmd_gui(args) -> int:
    from . import gui                                  # import tardio: PySide6 só no modo GUI
    # same_thread=False: a GUI roda as operações num pool de 1 thread (serializado).
    content, index, session = _build(args, same_thread=False)
    conn = config.ConnConfig.from_env(args.env)
    engine = SyncEngine(index)

    class _Controller:
        def status(self):
            return engine.status(content.scan(ignore_path=args.ignore), session._load()[1])

        def send(self, paths):
            scanned = content.scan(ignore_path=args.ignore)
            session.send_many([scanned[p] for p in paths if p in scanned])

        def receive(self, paths):
            for p in paths:
                session.receive(p)

        def delete(self, paths):
            for p in paths:
                session.delete(p)

        def add(self, local_paths):
            # resolve_or_register: se o arquivo está fora das raízes mas dentro de ~/,
            # registra a pasta (ou o avulso) no dirs.json antes de enviar.
            sfs = [scanner.scan_one(lp, content.resolve_or_register(lp)) for lp in local_paths]
            session.send_many(sfs)

        def add_start_dir(self):
            dirs = content.default_dirs
            return str(dirs[0][0]) if dirs else str(_HOME)

        def sync_folders(self):
            return {"dirs": list(content.default_dirs), "items": list(content.extra_items)}

        def add_root(self, path):
            return content.add_dir(path)

        def remove_root(self, path):
            return content.remove_dir(path)

        def remove_item(self, path):
            return content.remove_item(path)

        def ui_state(self):
            import json
            p = _DATA_DIR / "gui-state.json"
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}

        def save_ui_state(self, state):
            import json
            p = _DATA_DIR / "gui-state.json"
            try:
                p.parent.mkdir(parents=True, exist_ok=True)   # cria ~/.local/data/unuser
                p.write_text(json.dumps(state), encoding="utf-8")
            except OSError:
                pass

        def connection_label(self):
            if conn.mode == "tor":
                return f"Tor {conn.tor_onion or '(sem onion)'}"
            return f"Direto {conn.direct_host}:{conn.direct_port}"

    return gui.run(_Controller())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="unuser", description="Cliente do cofre-cego unuser.")
    p.add_argument("--env", default=DEFAULTS["env"], type=Path)
    p.add_argument("--dirs", default=DEFAULTS["dirs"], type=Path)
    p.add_argument("--ignore", default=DEFAULTS["ignore"], type=Path)
    p.add_argument("--index", default=DEFAULTS["index"], type=Path)
    p.add_argument("--keyfile", default=None)
    p.add_argument("--device", default=os.uname().nodename)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="mostra o status de cada arquivo").set_defaults(func=cmd_status)
    s = sub.add_parser("send", help="envia arquivos (sem alvos: todos os modificados)")
    s.add_argument("paths", nargs="*")
    s.set_defaults(func=cmd_send)
    r = sub.add_parser("receive", help="baixa arquivos do cofre")
    r.add_argument("paths", nargs="+")
    r.set_defaults(func=cmd_receive)
    d = sub.add_parser("delete", help="apaga arquivos (tombstone + remove local)")
    d.add_argument("paths", nargs="+")
    d.set_defaults(func=cmd_delete)
    sub.add_parser("gui", help="abre a interface gráfica (PySide6)").set_defaults(func=cmd_gui)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
