"""Testes da API HTTP do servidor (plain + mTLS real) via VaultClient."""

import threading
from contextlib import contextmanager

import pytest

from unuser import tls
from unuser.client.transport import ConflictError, TransportError, VaultClient
from unuser.server.http_server import make_server
from unuser.server.storage import BlindStorage

BID = "b:" + "ab" * 32


@contextmanager
def running(storage, ssl_context=None):
    httpd = make_server(storage, ssl_context=ssl_context)
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


# --- API sobre HTTP simples -------------------------------------------------

def test_health(store):
    with running(store) as port:
        assert VaultClient("127.0.0.1", port).health()


def test_manifest_ciclo_e_cas(store):
    with running(store) as port:
        c = VaultClient("127.0.0.1", port)
        assert c.get_manifest() == (0, b"")          # cofre novo
        c.put_manifest(0, 1, b"manifesto-cifrado-1")
        assert c.get_manifest() == (1, b"manifesto-cifrado-1")

        c.put_manifest(1, 2, b"manifesto-cifrado-2")
        assert c.get_manifest() == (2, b"manifesto-cifrado-2")

        with pytest.raises(ConflictError) as ei:     # baseado em versão obsoleta
            c.put_manifest(1, 3, b"perdido")
        assert ei.value.current == 2
        assert c.get_manifest() == (2, b"manifesto-cifrado-2")


def test_blobs(store):
    with running(store) as port:
        c = VaultClient("127.0.0.1", port)
        assert not c.has_blob(BID)
        c.put_blob(BID, b"bytes-cifrados")
        assert c.has_blob(BID)
        assert c.get_blob(BID) == b"bytes-cifrados"


def test_blob_inexistente_e_invalido(store):
    with running(store) as port:
        c = VaultClient("127.0.0.1", port)
        with pytest.raises(TransportError):
            c.get_blob(BID)                          # 404
        with pytest.raises(TransportError):
            c.get_blob("b:nao-hex")                  # 400


# --- mTLS --------------------------------------------------------------------

@pytest.fixture
def pki(tmp_path):
    """Gera certs de servidor e de dois clientes (um na allowlist, outro não)."""
    s_crt, s_key = tls.generate_self_signed("unuserd", tmp_path, "server")
    ok_crt, ok_key = tls.generate_self_signed("cliente-ok", tmp_path, "client_ok")
    bad_crt, bad_key = tls.generate_self_signed("cliente-intruso", tmp_path, "client_bad")
    allow = tls.write_allowlist([ok_crt], tmp_path / "allow.pem")  # só o "ok"
    return dict(s_crt=s_crt, s_key=s_key, ok_crt=ok_crt, ok_key=ok_key,
                bad_crt=bad_crt, bad_key=bad_key, allow=allow)


def test_mtls_cliente_autorizado(store, pki):
    sctx = tls.server_context(pki["s_crt"], pki["s_key"], pki["allow"])
    with running(store, ssl_context=sctx) as port:
        cctx = tls.client_context(pki["ok_crt"], pki["ok_key"], pki["s_crt"])
        c = VaultClient("127.0.0.1", port, ssl_context=cctx)
        assert c.health()
        c.put_manifest(0, 1, b"via-mtls")
        assert c.get_manifest() == (1, b"via-mtls")


def test_mtls_cliente_fora_da_allowlist_e_rejeitado(store, pki):
    sctx = tls.server_context(pki["s_crt"], pki["s_key"], pki["allow"])
    with running(store, ssl_context=sctx) as port:
        cctx = tls.client_context(pki["bad_crt"], pki["bad_key"], pki["s_crt"])
        c = VaultClient("127.0.0.1", port, ssl_context=cctx)
        with pytest.raises(Exception):              # handshake falha (cert não confiável)
            c.health()


def test_fingerprint_estavel(pki):
    fp1 = tls.cert_fingerprint(pki["ok_crt"])
    fp2 = tls.cert_fingerprint(pki["ok_crt"])
    assert fp1 == fp2 and len(fp1) == 64
    assert fp1 != tls.cert_fingerprint(pki["bad_crt"])
