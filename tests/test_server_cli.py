"""Testes do daemon unuserd (server.cli): parsing e servidor montado de fato."""

import threading

from client.transport import VaultClient
from server import cli


def test_parser_defaults():
    args = cli.build_parser().parse_args([])
    assert args.host == "127.0.0.1" and args.port == 8443
    assert args.storage.endswith("/vault")
    assert args.tls_cert is None


def test_build_server_serve_e_responde(tmp_path):
    """build_server monta um servidor funcional (sem mTLS) que responde no /healthz."""
    args = cli.build_parser().parse_args([
        "--storage", str(tmp_path / "vault"), "--host", "127.0.0.1", "--port", "0",
    ])
    httpd = cli.build_server(args)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        c = VaultClient("127.0.0.1", port)
        assert c.health()
        c.put_blob("b:" + "ab" * 32, b"opaco")
        assert c.get_blob("b:" + "ab" * 32) == b"opaco"
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def test_build_server_com_mtls(tmp_path):
    """Com os 3 arquivos TLS, o servidor é montado com contexto mTLS."""
    from common import tls

    s_crt, s_key = tls.generate_self_signed("unuserd", tmp_path, "server")
    ok_crt, _ = tls.generate_self_signed("cliente", tmp_path, "ok")
    allow = tls.write_allowlist([ok_crt], tmp_path / "allow.pem")
    args = cli.build_parser().parse_args([
        "--storage", str(tmp_path / "vault"), "--port", "0",
        "--tls-cert", str(s_crt), "--tls-key", str(s_key), "--tls-allow", str(allow),
    ])
    httpd = cli.build_server(args)
    try:
        assert httpd.socket.context.verify_mode.name == "CERT_REQUIRED"
    finally:
        httpd.server_close()
