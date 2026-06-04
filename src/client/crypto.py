"""Núcleo de criptografia do unuser.

Implementa a hierarquia de chaves descrita na especificação (seção 4):

    passphrase --Argon2id(salt)-->  kp
    keyfile (alta entropia)     -->  kf
    root_key = HKDF-SHA256(ikm = kp || kf)

    root_key --HKDF(label)-->  KEK           (embrulha as chaves de arquivo)
                               name_key      (cifra nomes/metadados no manifesto)
                               manifest_key  (cifra o manifesto)
                               block_id_key  (HMAC para deduplicação)

    Por arquivo:
        FK         = aleatória de 256 bits          (chave de conteúdo)
        blocos     = AES-256-GCM(FK,  bloco)
        wrapped_FK = AES-256-GCM(KEK, FK)           (guardada no manifesto)

Princípio: aqui só usamos primitivas consagradas (Argon2id, HKDF, AES-256-GCM,
HMAC-SHA256, BLAKE3). Nada de criptografia "caseira".
"""

from __future__ import annotations

import hmac as _hmac
import os
from dataclasses import dataclass

from argon2.low_level import Type as _Argon2Type
from argon2.low_level import hash_secret_raw as _argon2_raw
from blake3 import blake3 as _blake3
from cryptography.hazmat.primitives import hashes as _hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# --- parâmetros -------------------------------------------------------------

KEY_SIZE = 32  # 256 bits
NONCE_SIZE = 12  # AES-GCM
SALT_SIZE = 16

# Argon2id (custo de desbloqueio do cofre). memory_cost em KiB.
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 64 * 1024  # 64 MiB
ARGON2_PARALLELISM = 4

# Rótulos de domínio para o HKDF (separação de finalidade das subchaves).
_INFO_ROOT = b"unuser:v1:root"
_INFO_KEK = b"unuser:v1:kek"
_INFO_NAME = b"unuser:v1:name"
_INFO_MANIFEST = b"unuser:v1:manifest"
_INFO_BLOCK_ID = b"unuser:v1:block-id"

# AAD (dados autenticados) para separar contextos de cifragem.
_AAD_FK = b"unuser:v1:wrapped-fk"


# --- aleatoriedade ----------------------------------------------------------

def generate_salt() -> bytes:
    """Salt aleatório para o Argon2id (guardado no cabeçalho do cofre)."""
    return os.urandom(SALT_SIZE)


def generate_file_key() -> bytes:
    """FK: chave de conteúdo aleatória, exclusiva de cada arquivo."""
    return os.urandom(KEY_SIZE)


# --- derivação de chaves ----------------------------------------------------

def _hkdf(ikm: bytes, *, info: bytes, salt: bytes | None = None,
          length: int = KEY_SIZE) -> bytes:
    return HKDF(algorithm=_hashes.SHA256(), length=length, salt=salt,
                info=info).derive(ikm)


def derive_root_key(
    passphrase: str,
    keyfile: bytes,
    salt: bytes,
    *,
    time_cost: int = ARGON2_TIME_COST,
    memory_cost: int = ARGON2_MEMORY_COST,
    parallelism: int = ARGON2_PARALLELISM,
) -> bytes:
    """Deriva a root_key a partir de passphrase + keyfile (ambos obrigatórios).

    A passphrase passa por Argon2id; o resultado é combinado com o keyfile via
    HKDF. Sem o keyfile, a mesma senha gera uma root_key diferente — os dois
    fatores são necessários para abrir o cofre.
    """
    if not passphrase:
        raise ValueError("passphrase vazia")
    if not keyfile:
        raise ValueError("keyfile vazio")
    if len(salt) != SALT_SIZE:
        raise ValueError(f"salt deve ter {SALT_SIZE} bytes")

    kp = _argon2_raw(
        secret=passphrase.encode("utf-8"),
        salt=salt,
        time_cost=time_cost,
        memory_cost=memory_cost,
        parallelism=parallelism,
        hash_len=KEY_SIZE,
        type=_Argon2Type.ID,
    )
    return _hkdf(kp + keyfile, info=_INFO_ROOT, salt=salt)


@dataclass(frozen=True)
class Keyring:
    """Subchaves derivadas da root_key, por finalidade."""

    kek: bytes           # embrulha as FKs
    name_key: bytes      # cifra nomes/metadados
    manifest_key: bytes  # cifra o manifesto
    block_id_key: bytes  # HMAC para deduplicação


def derive_keyring(root_key: bytes) -> Keyring:
    """Expande a root_key nas subchaves de finalidade específica."""
    return Keyring(
        kek=_hkdf(root_key, info=_INFO_KEK),
        name_key=_hkdf(root_key, info=_INFO_NAME),
        manifest_key=_hkdf(root_key, info=_INFO_MANIFEST),
        block_id_key=_hkdf(root_key, info=_INFO_BLOCK_ID),
    )


def unlock(passphrase: str, keyfile: bytes, salt: bytes, **kw) -> Keyring:
    """Conveniência: deriva a root_key e devolve o Keyring pronto."""
    return derive_keyring(derive_root_key(passphrase, keyfile, salt, **kw))


# --- cifragem autenticada (AES-256-GCM) -------------------------------------

def encrypt(key: bytes, plaintext: bytes, aad: bytes | None = None) -> bytes:
    """Cifra com AES-256-GCM. Devolve nonce || ciphertext || tag."""
    nonce = os.urandom(NONCE_SIZE)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def decrypt(key: bytes, blob: bytes, aad: bytes | None = None) -> bytes:
    """Decifra o formato produzido por :func:`encrypt`.

    Levanta ``cryptography.exceptions.InvalidTag`` se a chave/AAD estiverem
    erradas ou o blob tiver sido adulterado.
    """
    if len(blob) < NONCE_SIZE:
        raise ValueError("blob curto demais")
    nonce, ct = blob[:NONCE_SIZE], blob[NONCE_SIZE:]
    return AESGCM(key).decrypt(nonce, ct, aad)


# --- envelope (chaves de arquivo) -------------------------------------------

def wrap_file_key(kek: bytes, fk: bytes) -> bytes:
    """Embrulha a FK com a KEK (o que vai guardado no manifesto)."""
    return encrypt(kek, fk, aad=_AAD_FK)


def unwrap_file_key(kek: bytes, wrapped_fk: bytes) -> bytes:
    """Desembrulha a FK com a KEK."""
    return decrypt(kek, wrapped_fk, aad=_AAD_FK)


# --- identificação de bloco e hash de conteúdo ------------------------------

def block_id(block_id_key: bytes, block: bytes) -> str:
    """Identificador determinístico e com chave de um bloco (deduplicação).

    Usa HMAC-SHA256: o servidor vê o id, mas não consegue inferir o conteúdo.
    """
    digest = _hmac.new(block_id_key, block, "sha256").hexdigest()
    return "b:" + digest


def content_hash(data: bytes) -> str:
    """Hash BLAKE3 do conteúdo (detecção de modificação e integridade)."""
    return "blake3:" + _blake3(data).hexdigest()


def content_hasher():
    """Hasher BLAKE3 incremental — mesmo digest de :func:`content_hash`, alimentado por
    partes (para hashear um arquivo em streaming sem carregá-lo inteiro).

    Uso: ``h = content_hasher(); h.update(parte); ...; "blake3:" + h.hexdigest()``.
    """
    return _blake3()
