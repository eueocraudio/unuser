"""Testes do núcleo de criptografia (unuser.crypto)."""

import pytest
from cryptography.exceptions import InvalidTag

from unuser import crypto

# Argon2 leve para os testes não ficarem lentos.
FAST = dict(time_cost=1, memory_cost=8 * 1024, parallelism=1)


@pytest.fixture
def salt():
    return crypto.generate_salt()


# --- derivação de chaves ----------------------------------------------------

def test_root_key_deterministica(salt):
    a = crypto.derive_root_key("senha-forte", b"keyfile-bytes", salt, **FAST)
    b = crypto.derive_root_key("senha-forte", b"keyfile-bytes", salt, **FAST)
    assert a == b
    assert len(a) == crypto.KEY_SIZE


def test_keyfile_e_obrigatorio_para_a_chave(salt):
    """Mudar só o keyfile muda a root_key: prova que os dois fatores contam."""
    base = crypto.derive_root_key("senha", b"keyfile-A", salt, **FAST)
    outro = crypto.derive_root_key("senha", b"keyfile-B", salt, **FAST)
    assert base != outro


def test_passphrase_muda_a_chave(salt):
    a = crypto.derive_root_key("senha-1", b"kf", salt, **FAST)
    b = crypto.derive_root_key("senha-2", b"kf", salt, **FAST)
    assert a != b


def test_salt_muda_a_chave():
    s1, s2 = crypto.generate_salt(), crypto.generate_salt()
    a = crypto.derive_root_key("senha", b"kf", s1, **FAST)
    b = crypto.derive_root_key("senha", b"kf", s2, **FAST)
    assert a != b


def test_passphrase_e_keyfile_vazios_falham(salt):
    with pytest.raises(ValueError):
        crypto.derive_root_key("", b"kf", salt, **FAST)
    with pytest.raises(ValueError):
        crypto.derive_root_key("senha", b"", salt, **FAST)


def test_subchaves_sao_distintas(salt):
    root = crypto.derive_root_key("senha", b"kf", salt, **FAST)
    kr = crypto.derive_keyring(root)
    chaves = {kr.kek, kr.name_key, kr.manifest_key, kr.block_id_key}
    assert len(chaves) == 4  # nenhuma colide
    assert all(len(k) == crypto.KEY_SIZE for k in chaves)


def test_unlock_equivale_a_derivar_em_dois_passos(salt):
    kr1 = crypto.unlock("senha", b"kf", salt, **FAST)
    kr2 = crypto.derive_keyring(crypto.derive_root_key("senha", b"kf", salt, **FAST))
    assert kr1 == kr2


# --- AES-256-GCM ------------------------------------------------------------

def test_encrypt_decrypt_roundtrip():
    key = crypto.generate_file_key()
    msg = b"conteudo secreto com acentuacao: cofre"
    blob = crypto.encrypt(key, msg)
    assert crypto.decrypt(key, blob) == msg


def test_nonce_aleatorio_por_operacao():
    key = crypto.generate_file_key()
    msg = b"mesmo texto"
    assert crypto.encrypt(key, msg) != crypto.encrypt(key, msg)


def test_chave_errada_falha():
    blob = crypto.encrypt(crypto.generate_file_key(), b"x")
    with pytest.raises(InvalidTag):
        crypto.decrypt(crypto.generate_file_key(), blob)


def test_blob_adulterado_falha():
    key = crypto.generate_file_key()
    blob = bytearray(crypto.encrypt(key, b"importante"))
    blob[-1] ^= 0x01  # vira um bit da tag
    with pytest.raises(InvalidTag):
        crypto.decrypt(key, bytes(blob))


def test_aad_precisa_bater():
    key = crypto.generate_file_key()
    blob = crypto.encrypt(key, b"x", aad=b"ctx-A")
    with pytest.raises(InvalidTag):
        crypto.decrypt(key, blob, aad=b"ctx-B")


# --- envelope (FK) ----------------------------------------------------------

def test_wrap_unwrap_fk(salt):
    kek = crypto.unlock("senha", b"kf", salt, **FAST).kek
    fk = crypto.generate_file_key()
    wrapped = crypto.wrap_file_key(kek, fk)
    assert wrapped != fk
    assert crypto.unwrap_file_key(kek, wrapped) == fk


def test_unwrap_com_kek_errada_falha(salt):
    fk = crypto.generate_file_key()
    kek_a = crypto.unlock("senha-A", b"kf", salt, **FAST).kek
    kek_b = crypto.unlock("senha-B", b"kf", salt, **FAST).kek
    wrapped = crypto.wrap_file_key(kek_a, fk)
    with pytest.raises(InvalidTag):
        crypto.unwrap_file_key(kek_b, wrapped)


def test_rotacao_reenvelopa_sem_tocar_conteudo(salt):
    """Simula a rotação (4.2): re-embrulhar a FK não muda o conteúdo cifrado."""
    fk = crypto.generate_file_key()
    conteudo = crypto.encrypt(fk, b"um documento qualquer")

    kek_velha = crypto.unlock("senha-velha", b"kf", salt, **FAST).kek
    kek_nova = crypto.unlock("senha-nova", b"kf", salt, **FAST).kek

    wrapped_v = crypto.wrap_file_key(kek_velha, fk)
    fk_recuperada = crypto.unwrap_file_key(kek_velha, wrapped_v)
    wrapped_n = crypto.wrap_file_key(kek_nova, fk_recuperada)

    # A FK sob a nova KEK ainda decifra o MESMO conteúdo, sem recriptografá-lo.
    fk_final = crypto.unwrap_file_key(kek_nova, wrapped_n)
    assert crypto.decrypt(fk_final, conteudo) == b"um documento qualquer"


# --- block_id e hash --------------------------------------------------------

def test_block_id_deterministico_e_com_chave():
    k1 = crypto.generate_file_key()
    k2 = crypto.generate_file_key()
    bloco = b"bloco de dados"
    assert crypto.block_id(k1, bloco) == crypto.block_id(k1, bloco)
    assert crypto.block_id(k1, bloco) != crypto.block_id(k2, bloco)
    assert crypto.block_id(k1, bloco).startswith("b:")


def test_block_id_difere_por_conteudo():
    k = crypto.generate_file_key()
    assert crypto.block_id(k, b"a") != crypto.block_id(k, b"b")


def test_content_hash():
    assert crypto.content_hash(b"abc") == crypto.content_hash(b"abc")
    assert crypto.content_hash(b"abc") != crypto.content_hash(b"abd")
    assert crypto.content_hash(b"abc").startswith("blake3:")
