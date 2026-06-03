"""E2E pelo CLI: status → send → receive → delete contra um servidor real."""

import threading
from contextlib import contextmanager

import pytest

from client import cli
from server.http_server import make_server
from server.storage import BlindStorage


@contextmanager
def running(storage):
    httpd = make_server(storage)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """Monta env/dirs/keyfile/índice e devolve um runner do CLI já parametrizado."""
    # Argon2 leve + passphrase via env (sem prompt).
    monkeypatch.setenv("UNUSER_PASSPHRASE", "senha-de-teste")
    keyfile = tmp_path / "keyfile.bin"
    keyfile.write_bytes(b"keyfile-de-alta-entropia")

    root = tmp_path / "Documentos"
    root.mkdir()
    (root / "nota.txt").write_bytes(b"conteudo da nota " * 30)

    dirs = tmp_path / "dirs.json"
    dirs.write_text(f'{{"default_dirs":[{{"path":"{root}","recursive":true}}]}}')
    index = tmp_path / "index.db"

    def make_runner(port, capsys):
        env = tmp_path / ".env"
        env.write_text(
            "UNUSER_VAULT_ID=v:cli\nUNUSER_CONN_MODE=direct\n"
            f"UNUSER_DIRECT_HOST=127.0.0.1\nUNUSER_DIRECT_PORT={port}\n"
        )

        def run(*cli_args) -> str:
            rc = cli.main([
                "--env", str(env), "--dirs", str(dirs), "--index", str(index),
                "--keyfile", str(keyfile), "--device", "pc-cli", *cli_args,
            ])
            assert rc == 0
            return capsys.readouterr().out

        return run

    return dict(make_runner=make_runner, root=root)


def test_cli_status_send_receive_delete(setup, tmp_path, capsys):
    store = BlindStorage(tmp_path / "vault")
    root = setup["root"]
    with running(store) as port:
        run = setup["make_runner"](port, capsys)

        # 1. status inicial: só local.
        out = run("status")
        assert "Só local" in out and "Documentos/nota.txt" in out

        # 2. send → 3. status em sincronia.
        assert "enviados 1" in run("send")
        assert "Em sincronia" in run("status")

        # 4. apaga o arquivo local e recupera com receive.
        (root / "nota.txt").unlink()
        out = run("receive", "Documentos/nota.txt")
        assert "recebido:" in out
        assert (root / "nota.txt").read_bytes() == b"conteudo da nota " * 30

        # 5. delete: some localmente (e vira tombstone no servidor).
        run("delete", "Documentos/nota.txt")
        assert not (root / "nota.txt").exists()
        out = run("receive", "Documentos/nota.txt")
        assert "nada a receber" in out
