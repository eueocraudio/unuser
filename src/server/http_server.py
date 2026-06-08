"""API HTTP do servidor cofre-cego (apenas stdlib).

Rotas:

* ``GET  /healthz``            → ``ok``
* ``GET  /manifest``           → corpo = blob cifrado; header ``X-Unuser-Version``
                                  (versão 0 e corpo vazio = cofre ainda sem manifesto)
* ``PUT  /manifest``           → headers ``X-Unuser-Expected-Version`` e
                                  ``X-Unuser-New-Version``; corpo = blob. 409 em conflito (CAS)
* ``HEAD /blob/<block_id>``    → 200 se existe, 404 se não
* ``GET  /blob/<block_id>``    → corpo = bytes cifrados do bloco
* ``PUT  /blob/<block_id>``    → corpo = bytes cifrados; 201
* ``GET  /usage``              → JSON do uso de disco (físico) e tamanho do cofre
* ``GET  /backups``            → JSON {backups: [...]} disponíveis no servidor
* ``POST /backups``            → cria um backup (server-side); 201 + JSON {name, ...}
* ``POST /restore``            → corpo JSON {name}; restaura (DESTRUTIVO) + JSON {version}

O servidor nunca decifra nada; só move bytes opacos.
"""

from __future__ import annotations

import json
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from .storage import BlindStorage, ConflictError, InvalidIdError, NotFoundError, StorageError

_BLOB_PREFIX = "/blob/"
_H_VERSION = "X-Unuser-Version"
_H_EXPECTED = "X-Unuser-Expected-Version"
_H_NEW = "X-Unuser-New-Version"
MAX_BODY = 256 * 1024 * 1024   # teto de corpo (anti-DoS); blobs reais são bem menores


class _BodyError(Exception):
    """Content-Length ausente/negativo/grande demais — já respondido com 400."""


class _Handler(BaseHTTPRequestHandler):
    server_version = "unuser/0.1"
    protocol_version = "HTTP/1.1"

    @property
    def storage(self) -> BlindStorage:
        return self.server.storage  # type: ignore[attr-defined]

    # --- helpers ------------------------------------------------------------

    def _send(self, code: int, body: bytes = b"", headers: dict | None = None) -> None:
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _body(self) -> bytes:
        raw = self.headers.get("Content-Length")
        if raw is None:
            return b""
        try:
            n = int(raw)
        except ValueError:
            n = -1
        # Negativo viraria read(-1) = ler até EOF (trava a thread em keep-alive);
        # gigante aloca/bloqueia. Recusa e fecha a conexão agora não-confiável.
        if n < 0 or n > MAX_BODY:
            self._send(400, b"content-length invalido", {"Connection": "close"})
            self.close_connection = True
            raise _BodyError
        return self.rfile.read(n) if n else b""

    def _block_id(self) -> str:
        return unquote(self.path[len(_BLOB_PREFIX):])

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"),
                   {"Content-Type": "application/json"})

    def log_message(self, *args) -> None:  # silencia o log padrão em stderr
        pass

    # --- métodos ------------------------------------------------------------

    def do_GET(self) -> None:
        if self.path == "/healthz":
            return self._send(200, b"ok")
        if self.path == "/manifest":
            res = self.storage.get_manifest()
            version, blob = res if res else (0, b"")
            return self._send(200, blob, {_H_VERSION: version})
        if self.path == "/usage":
            return self._json(200, self.storage.disk_usage())
        if self.path == "/backups":
            return self._json(200, {"backups": self.storage.list_backups()})
        if self.path.startswith(_BLOB_PREFIX):
            try:
                return self._send(200, self.storage.get_blob(self._block_id()))
            except InvalidIdError:
                return self._send(400, b"block_id invalido")
            except NotFoundError:
                return self._send(404)
        self._send(404)

    def do_HEAD(self) -> None:
        if self.path.startswith(_BLOB_PREFIX):
            try:
                exists = self.storage.has_blob(self._block_id())
            except InvalidIdError:
                return self._send(400)
            return self._send(200 if exists else 404)
        self._send(404)

    def do_PUT(self) -> None:
        try:
            body = self._body()
        except _BodyError:
            return
        if self.path == "/manifest":
            try:
                expected = int(self.headers[_H_EXPECTED])
                new = int(self.headers[_H_NEW])
            except (KeyError, TypeError, ValueError):
                return self._send(400, b"headers de versao ausentes/invalidos")
            try:
                self.storage.put_manifest(expected, new, body)
            except ConflictError as e:
                return self._send(409, b"conflito", {_H_VERSION: e.current})
            except StorageError:
                return self._send(400, b"erro de armazenamento")
            return self._send(200, b"ok")
        if self.path.startswith(_BLOB_PREFIX):
            try:
                self.storage.put_blob(self._block_id(), body)
            except InvalidIdError:
                return self._send(400, b"block_id invalido")
            return self._send(201, b"ok")
        self._send(404)

    def do_POST(self) -> None:
        try:
            body = self._body()
        except _BodyError:
            return
        if self.path == "/backups":                  # exporta (cria backup server-side)
            return self._json(201, self.storage.export())
        if self.path == "/restore":                  # importa (restaura — DESTRUTIVO)
            try:
                name = json.loads(body or b"{}").get("name")
            except (ValueError, AttributeError):
                return self._send(400, b"json invalido")
            if not name:
                return self._send(400, b"name ausente")
            try:
                version = self.storage.restore(name)
            except InvalidIdError:
                return self._send(400, b"nome de backup invalido")
            except NotFoundError:
                return self._send(404, b"backup inexistente")
            except StorageError:
                return self._send(400, b"erro ao restaurar")
            return self._json(200, {"version": version})
        self._send(404)


def make_server(storage: BlindStorage, host: str = "127.0.0.1", port: int = 0,
                ssl_context: ssl.SSLContext | None = None) -> ThreadingHTTPServer:
    """Cria o servidor. ``port=0`` escolhe uma porta livre (útil em testes).

    Passe ``ssl_context`` (de :func:`common.tls.server_context`) para habilitar mTLS.
    """
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.storage = storage  # type: ignore[attr-defined]
    if ssl_context is not None:
        httpd.socket = ssl_context.wrap_socket(httpd.socket, server_side=True)
    return httpd
