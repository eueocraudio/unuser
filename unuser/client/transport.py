"""Cliente HTTP(S) para o servidor cofre-cego.

Fala a API do :mod:`unuser.server.http_server`. Aceita um ``ssl_context`` de
:func:`unuser.tls.client_context` para mTLS. Na LAN conecta direto por IP; fora
da LAN, a mesma conexão pode ser tunelada pelo Tor (proxy SOCKS) — isso entra na
Fase 5, junto com a escolha IP/Tor na interface.
"""

from __future__ import annotations

import http.client
import ssl

_H_VERSION = "X-Unuser-Version"
_H_EXPECTED = "X-Unuser-Expected-Version"
_H_NEW = "X-Unuser-New-Version"


class TransportError(Exception):
    """Resposta inesperada do servidor."""


class ConflictError(TransportError):
    """O servidor rejeitou o manifesto por conflito de versão (CAS)."""

    def __init__(self, current: int):
        super().__init__(f"conflito de versão; atual no servidor = {current}")
        self.current = current


class VaultClient:
    def __init__(self, host: str, port: int, *, ssl_context: ssl.SSLContext | None = None,
                 timeout: float = 30.0):
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.timeout = timeout

    def _conn(self) -> http.client.HTTPConnection:
        if self.ssl_context is not None:
            return http.client.HTTPSConnection(
                self.host, self.port, context=self.ssl_context, timeout=self.timeout)
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def _request(self, method: str, path: str, body: bytes | None = None,
                 headers: dict | None = None):
        conn = self._conn()
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, dict(resp.getheaders()), data
        finally:
            conn.close()

    # --- API ----------------------------------------------------------------

    def health(self) -> bool:
        status, _, _ = self._request("GET", "/healthz")
        return status == 200

    def get_manifest(self) -> tuple[int, bytes]:
        """(version, blob). version 0 + blob vazio = cofre ainda sem manifesto."""
        status, headers, data = self._request("GET", "/manifest")
        if status != 200:
            raise TransportError(f"GET /manifest -> {status}")
        return int(headers.get(_H_VERSION, 0)), data

    def put_manifest(self, expected_version: int, new_version: int, blob: bytes) -> None:
        status, headers, _ = self._request(
            "PUT", "/manifest", body=blob,
            headers={_H_EXPECTED: str(expected_version), _H_NEW: str(new_version)},
        )
        if status == 409:
            raise ConflictError(int(headers.get(_H_VERSION, 0)))
        if status != 200:
            raise TransportError(f"PUT /manifest -> {status}")

    def has_blob(self, block_id: str) -> bool:
        status, _, _ = self._request("HEAD", f"/blob/{block_id}")
        return status == 200

    def get_blob(self, block_id: str) -> bytes:
        status, _, data = self._request("GET", f"/blob/{block_id}")
        if status != 200:
            raise TransportError(f"GET /blob -> {status}")
        return data

    def put_blob(self, block_id: str, data: bytes) -> None:
        status, _, _ = self._request("PUT", f"/blob/{block_id}", body=data)
        if status not in (200, 201):
            raise TransportError(f"PUT /blob -> {status}")
