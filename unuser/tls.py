"""Geração de certificados e contextos mTLS (TLS mútuo).

Modelo (seção 4): cada nó tem um par de chaves/certificado autoassinado. A
identidade do dispositivo é o *fingerprint* do certificado. O servidor só aceita
clientes cujos certificados estejam na **allowlist** (um arquivo PEM com os certs
confiáveis); o cliente, por sua vez, só confia no certificado do servidor.

Fora da LAN, este mesmo mTLS roda por dentro do Onion Service do Tor — o endereço
``.onion`` autentica o servidor e o mTLS autentica o cliente.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def generate_self_signed(common_name: str, out_dir, name: str) -> tuple[Path, Path]:
    """Gera um par cert/chave EC autoassinado. Devolve (cert_path, key_path)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=1))
        .not_valid_after(now + _dt.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    cert_path = out / f"{name}.crt"
    key_path = out / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def cert_fingerprint(cert_path) -> str:
    """SHA-256 (hex) do certificado em DER — a identidade do dispositivo."""
    pem = Path(cert_path).read_bytes()
    cert = x509.load_pem_x509_certificate(pem)
    der = cert.public_bytes(serialization.Encoding.DER)
    return hashlib.sha256(der).hexdigest()


def write_allowlist(cert_paths, out_path) -> Path:
    """Concatena os certs de clientes confiáveis num único arquivo PEM (allowlist)."""
    out = Path(out_path)
    out.write_bytes(b"".join(Path(p).read_bytes() for p in cert_paths))
    return out


def server_context(certfile, keyfile, client_allowlist) -> ssl.SSLContext:
    """Contexto do servidor: apresenta seu cert e **exige** cert de cliente confiável."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=str(client_allowlist))
    return ctx


def client_context(certfile, keyfile, server_cert) -> ssl.SSLContext:
    """Contexto do cliente: apresenta seu cert e confia só no cert do servidor."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
    ctx.load_verify_locations(cafile=str(server_cert))
    # Sem hostname: identidade é por cert (e, fora da LAN, pelo endereço .onion).
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx
