"""Daemon do servidor cofre-cego — o executável `unuserd`.

Sobe o :func:`server.http_server.make_server` sobre um :class:`BlindStorage` em disco.
Escuta TLS só em localhost (o Onion Service do Tor publica o serviço — ver
doc/operacao-tor.md); o mTLS é opcional e ligado quando os três arquivos de cert/chave/
allowlist são informados. Importa `common.tls` (cryptography) só nesse caso — um servidor
sem mTLS roda apenas com a stdlib.
"""

from __future__ import annotations

import argparse
import signal
import sys

from .http_server import make_server
from .storage import BlindStorage


def build_server(args: argparse.Namespace):
    """Monta o ThreadingHTTPServer a partir dos argumentos (separado de main p/ teste)."""
    ssl_context = None
    if args.tls_cert and args.tls_key and args.tls_allow:
        from common import tls           # import tardio: cryptography só com mTLS
        ssl_context = tls.server_context(args.tls_cert, args.tls_key, args.tls_allow)
    return make_server(BlindStorage(args.storage), host=args.host, port=args.port,
                       ssl_context=ssl_context)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="unuserd",
                                description="Servidor cofre-cego (zero-knowledge) do unuser.")
    p.add_argument("--storage", default="/var/lib/unuser/vault",
                   help="diretório do cofre (blobs + manifesto)")
    p.add_argument("--host", default="127.0.0.1", help="endereço de escuta (default: localhost)")
    p.add_argument("--port", type=int, default=8443, help="porta de escuta")
    p.add_argument("--tls-cert", help="cert do servidor (PEM) — liga mTLS se os 3 forem dados")
    p.add_argument("--tls-key", help="chave do servidor (PEM)")
    p.add_argument("--tls-allow", help="allowlist PEM dos certs de cliente confiáveis")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    httpd = build_server(args)
    host, port = httpd.server_address[:2]
    mtls = bool(args.tls_cert and args.tls_key and args.tls_allow)
    print(f"unuserd: cofre-cego em {'https' if mtls else 'http'}://{host}:{port} "
          f"(storage={args.storage}, mTLS={'on' if mtls else 'off'})", flush=True)

    signal.signal(signal.SIGTERM, signal.default_int_handler)  # SIGTERM (systemd stop) → KeyboardInterrupt
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
