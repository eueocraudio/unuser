"""Testa o túnel SOCKS5 do VaultClient com um proxy SOCKS5 mínimo (de verdade)
encaminhando para o servidor cofre-cego — sem precisar do Tor instalado."""

import socket
import threading
from contextlib import contextmanager

import pytest

from client.transport import TransportError, VaultClient, socks5_connect
from server.http_server import make_server
from server.storage import BlindStorage

BID = "b:" + "ab" * 32


def _pump(a: socket.socket, b: socket.socket):
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class _FakeSocks5:
    """SOCKS5 no-auth mínimo: faz o handshake, lê o CONNECT (ATYP=domínio) e liga os
    dois lados. Conta quantas conexões passaram, provando que o túnel foi usado."""

    def __init__(self):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.conexoes = 0
        self._stop = False
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self):
        while not self._stop:
            try:
                cli, _ = self.srv.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(cli,), daemon=True).start()

    def _handle(self, cli: socket.socket):
        # greeting
        ver, n = cli.recv(2)
        cli.recv(n)                                   # métodos
        cli.sendall(b"\x05\x00")                      # no-auth
        # request: VER CMD RSV ATYP
        _ver, _cmd, _rsv, atyp = cli.recv(4)
        host = cli.recv(cli.recv(1)[0]).decode() if atyp == 0x03 else None
        port = int.from_bytes(cli.recv(2), "big")
        upstream = socket.create_connection((host, port))
        cli.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")  # sucesso
        self.conexoes += 1
        threading.Thread(target=_pump, args=(cli, upstream), daemon=True).start()
        _pump(upstream, cli)

    def close(self):
        self._stop = True
        try:
            self.srv.close()
        except OSError:
            pass


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
def store(tmp_path):
    return BlindStorage(tmp_path / "vault")


def test_vaultclient_via_socks5(store):
    """O cliente fala com o servidor inteiramente através do proxy SOCKS5."""
    socks = _FakeSocks5()
    try:
        with running(store) as port:
            c = VaultClient("127.0.0.1", port, socks_proxy=("127.0.0.1", socks.port))
            assert c.health()
            c.put_blob(BID, b"bytes-cifrados-via-tor")
            assert c.get_blob(BID) == b"bytes-cifrados-via-tor"
            c.put_manifest(0, 1, b"manifesto-via-tor")
            assert c.get_manifest() == (1, b"manifesto-via-tor")
        assert socks.conexoes >= 1                    # tudo passou pelo túnel
    finally:
        socks.close()


def test_socks5_connect_erro_de_proxy():
    """Sem proxy escutando, o handshake falha de forma limpa."""
    livre = socket.socket()
    livre.bind(("127.0.0.1", 0))
    porta = livre.getsockname()[1]
    livre.close()                                     # ninguém escutando aqui
    with pytest.raises((TransportError, OSError)):
        socks5_connect(("127.0.0.1", porta), ("x.onion", 8443), timeout=2)
