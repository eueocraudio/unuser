"""Cliente HTTP(S) para o servidor cofre-cego.

Fala a API do :mod:`server.http_server`. Aceita um ``ssl_context`` de
:func:`common.tls.client_context` para mTLS. Na LAN conecta direto por IP; fora da LAN,
passe ``socks_proxy=(host, port)`` para tunelar a mesma conexão pelo SOCKS5 do Tor —
o mTLS continua autenticando o cliente por dentro do túnel, e o ``.onion`` autentica o
servidor (§4.6).
"""

from __future__ import annotations

import http.client
import socket
import ssl
import threading

_H_VERSION = "X-Unuser-Version"
_H_EXPECTED = "X-Unuser-Expected-Version"
_H_NEW = "X-Unuser-New-Version"


class TransportError(Exception):
    """Resposta inesperada do servidor."""


# --- túnel SOCKS5 (Tor) -----------------------------------------------------

def _recvn(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise TransportError("conexão SOCKS5 fechada no meio do handshake")
        buf += chunk
    return buf


def socks5_connect(proxy: tuple[str, int], dest: tuple[str, int],
                   timeout: float | None = None) -> socket.socket:
    """Abre um socket TCP até ``dest`` através de um proxy SOCKS5 (sem autenticação).

    Usa ATYP=domínio (0x03) e deixa o **proxy** resolver o host — é assim que o Tor
    alcança um endereço ``.onion`` (o cliente não o resolve nem deveria).
    """
    proxy_host, proxy_port = proxy
    dest_host, dest_port = dest
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        sock.sendall(b"\x05\x01\x00")                  # VER=5, NMETHODS=1, MÉTODO=no-auth
        ver, method = _recvn(sock, 2)
        if ver != 0x05 or method != 0x00:
            raise TransportError("SOCKS5: proxy não aceitou 'sem autenticação'")

        host_b = dest_host.encode("ascii")             # .onion é ASCII; Tor resolve
        if not host_b or len(host_b) > 255:
            raise TransportError(f"SOCKS5: host inválido {dest_host!r}")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b
                     + int(dest_port).to_bytes(2, "big"))

        ver, rep, _rsv, atyp = _recvn(sock, 4)         # VER, REP, RSV, ATYP
        if rep != 0x00:
            raise TransportError(f"SOCKS5: CONNECT recusado (REP={rep})")
        if atyp == 0x01:                               # IPv4: descarta BND.ADDR
            _recvn(sock, 4)
        elif atyp == 0x04:                             # IPv6
            _recvn(sock, 16)
        elif atyp == 0x03:                             # domínio (1 byte de tamanho)
            _recvn(sock, _recvn(sock, 1)[0])
        else:
            raise TransportError(f"SOCKS5: ATYP inesperado {atyp}")
        _recvn(sock, 2)                                # descarta BND.PORT
        return sock
    except Exception:
        sock.close()
        raise


class _Socks5HTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, socks: tuple[str, int], **kwargs):
        self._socks = socks
        super().__init__(*args, **kwargs)

    def connect(self) -> None:
        self.sock = socks5_connect(self._socks, (self.host, self.port), self.timeout)


class _Socks5HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, socks: tuple[str, int], **kwargs):
        self._socks = socks
        super().__init__(*args, **kwargs)

    def connect(self) -> None:
        raw = socks5_connect(self._socks, (self.host, self.port), self.timeout)
        # mTLS por dentro do túnel; check_hostname=False no client_context (id por cert).
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class ConflictError(TransportError):
    """O servidor rejeitou o manifesto por conflito de versão (CAS)."""

    def __init__(self, current: int):
        super().__init__(f"conflito de versão; atual no servidor = {current}")
        self.current = current


class VaultClient:
    def __init__(self, host: str, port: int, *, ssl_context: ssl.SSLContext | None = None,
                 socks_proxy: tuple[str, int] | None = None, timeout: float = 30.0):
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.socks_proxy = socks_proxy        # (host, port) do SOCKS5 do Tor, ou None
        self.timeout = timeout
        # Conexão persistente (keep-alive). Enviar um arquivo grande são milhares de
        # blocos: abrir/fechar uma conexão TCP por bloco esgota portas efêmeras
        # (acúmulo de TIME_WAIT) e faz uma conexão posterior travar até o timeout. O
        # servidor fala HTTP/1.1 com Content-Length, então reusamos um único socket.
        self._cached: http.client.HTTPConnection | None = None
        self._lock = threading.Lock()         # uso serializado (CLI 1 thread; GUI pool de 1)

    def _new_conn(self) -> http.client.HTTPConnection:
        if self.socks_proxy is not None:      # tunelado pelo Tor
            if self.ssl_context is not None:
                return _Socks5HTTPSConnection(
                    self.host, self.port, context=self.ssl_context,
                    timeout=self.timeout, socks=self.socks_proxy)
            return _Socks5HTTPConnection(
                self.host, self.port, timeout=self.timeout, socks=self.socks_proxy)
        if self.ssl_context is not None:
            return http.client.HTTPSConnection(
                self.host, self.port, context=self.ssl_context, timeout=self.timeout)
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def _close(self) -> None:
        if self._cached is not None:
            try:
                self._cached.close()
            except Exception:
                pass
            self._cached = None

    def close(self) -> None:
        """Fecha a conexão persistente (idempotente)."""
        with self._lock:
            self._close()

    def _request(self, method: str, path: str, body: bytes | None = None,
                 headers: dict | None = None):
        with self._lock:
            # Reusa a conexão; se ela estiver morta (servidor fechou, idle expirou,
            # pipe quebrado), reconecta UMA vez e repete o request.
            for attempt in (1, 2):
                if self._cached is None:
                    self._cached = self._new_conn()
                    self._cached.connect()
                    # Sem Nagle: numa conexão persistente com muitos request/response
                    # pequenos (1 por bloco), Nagle + delayed-ACK adicionam ~40ms por
                    # request. TCP_NODELAY corta esse stall (medido: 203s → poucos s).
                    try:
                        self._cached.sock.setsockopt(
                            socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    except (OSError, AttributeError):
                        pass
                try:
                    self._cached.request(method, path, body=body, headers=headers or {})
                    resp = self._cached.getresponse()
                    data = resp.read()                 # esvazia → socket pronto p/ reuso
                    return resp.status, dict(resp.getheaders()), data
                except (http.client.HTTPException, OSError):
                    self._close()                      # conexão suja → descarta
                    if attempt == 2:
                        raise

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
