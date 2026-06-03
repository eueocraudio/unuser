"""Testes do manifesto (modelo, serialização, cifragem, versionamento, rotação)."""

import pytest
from cryptography.exceptions import InvalidTag

from client import chunker, crypto, manifest
from client.manifest import VaultManifest

FAST = dict(time_cost=1, memory_cost=8 * 1024, parallelism=1)


@pytest.fixture
def keyring():
    salt = crypto.generate_salt()
    return salt, crypto.unlock("senha", b"keyfile", salt, **FAST)


@pytest.fixture
def vault(keyring):
    salt, _ = keyring
    return VaultManifest.new("v:abc", salt)


def _record(vault, kek, path="Documentos/a.txt", data=b"conteudo do arquivo " * 100,
            device="pc-trabalho"):
    fk = crypto.generate_file_key()
    blocks = chunker.split(data, crypto.generate_file_key())
    return vault.record_version(
        path=path, fk=fk, kek=kek, device=device,
        content_hash=crypto.content_hash(data), size=len(data), blocks=blocks,
    ), fk


# --- modelo / versionamento -------------------------------------------------

def test_vault_novo_vazio(vault):
    assert vault.files == {}
    assert vault.vault_version == 0
    assert vault.kek_epoch == 1


def test_record_cria_arquivo_e_incrementa_versao(vault, keyring):
    _, kr = keyring
    fm, _ = _record(vault, kr.kek)
    assert fm.file_id in vault.files
    assert fm.path == "Documentos/a.txt"
    assert len(fm.versions) == 1
    assert vault.vault_version == 1
    assert fm.wrapped_by_epoch == 1


def test_file_key_recupera_a_fk(vault, keyring):
    _, kr = keyring
    fm, fk = _record(vault, kr.kek)
    assert vault.file_key(fm, kr.kek) == fk


def test_segunda_versao_mantem_id_e_fk(vault, keyring):
    _, kr = keyring
    fm1, fk1 = _record(vault, kr.kek, data=b"v1 " * 100)
    fk_wrapped_antes = fm1.wrapped_fk
    fm2, _ = _record(vault, kr.kek, data=b"v2 mudou " * 100)
    assert fm1.file_id == fm2.file_id
    assert len(fm2.versions) == 2
    assert fm2.wrapped_fk == fk_wrapped_antes      # FK por arquivo não muda
    assert vault.file_key(fm2, kr.kek) == fk1
    assert vault.vault_version == 2


def test_versoes_ordenam_por_ts(vault, keyring):
    _, kr = keyring
    _record(vault, kr.kek, data=b"a" * 100)
    fm = vault.file_by_path("Documentos/a.txt")
    fm.versions[0].ts = "2026-06-01 09:00:00"
    fm.versions.append(type(fm.versions[0])(ts="2026-06-03 16:42:07", device="x",
                                            hash="blake3:z", size=1, blocks=[]))
    ordenado = sorted(fm.versions, key=lambda v: v.ts)
    assert ordenado[-1].ts == "2026-06-03 16:42:07"


def test_tombstone_do_apagar(vault, keyring):
    _, kr = keyring
    _record(vault, kr.kek)
    assert vault.file_by_path("Documentos/a.txt").deleted is False
    fm = vault.mark_deleted(path="Documentos/a.txt", device="pc-trabalho")
    assert fm.deleted is True
    assert len(fm.versions) == 2          # histórico preservado
    assert vault.vault_version == 2


def test_mark_deleted_inexistente(vault):
    with pytest.raises(KeyError):
        vault.mark_deleted(path="nao/existe", device="x")


def test_now_ts_formato():
    ts = manifest.now_ts()
    assert len(ts) == 19 and ts[4] == "-" and ts[10] == " " and ts[13] == ":"


# --- serialização -----------------------------------------------------------

def test_roundtrip_dict(vault, keyring):
    _, kr = keyring
    _record(vault, kr.kek)
    vault.mark_deleted(path="Documentos/a.txt", device="x")
    restaurado = VaultManifest.from_dict(vault.to_dict())
    assert restaurado.to_dict() == vault.to_dict()


def test_dumps_canonico_estavel(vault, keyring):
    _, kr = keyring
    _record(vault, kr.kek)
    assert manifest.dumps(vault) == manifest.dumps(VaultManifest.from_dict(vault.to_dict()))


# --- cifragem ---------------------------------------------------------------

def test_seal_open_roundtrip(vault, keyring):
    _, kr = keyring
    _record(vault, kr.kek)
    blob = manifest.seal(vault, kr.manifest_key)
    aberto = manifest.open_sealed(blob, kr.manifest_key)
    assert aberto.to_dict() == vault.to_dict()


def test_seal_nao_vaza_o_path(vault, keyring):
    _, kr = keyring
    _record(vault, kr.kek, path="Documentos/segredo-no-nome.txt")
    blob = manifest.seal(vault, kr.manifest_key)
    assert b"segredo-no-nome" not in blob


def test_open_com_chave_errada_falha(vault, keyring):
    _, kr = keyring
    _record(vault, kr.kek)
    blob = manifest.seal(vault, kr.manifest_key)
    with pytest.raises(InvalidTag):
        manifest.open_sealed(blob, crypto.generate_file_key())


# --- rotação da chave-mestra ------------------------------------------------

def test_rotacao_reembrulha_sem_tocar_conteudo(vault):
    """Headline: girar a KEK re-embrulha as FKs; o conteúdo cifrado continua válido."""
    salt = crypto.generate_salt()
    kek_velha = crypto.unlock("senha-velha", b"kf", salt, **FAST).kek
    kek_nova = crypto.unlock("senha-nova", b"kf", salt, **FAST).kek

    # arquivo cujo conteúdo é cifrado com a FK
    fk = crypto.generate_file_key()
    conteudo_cifrado = crypto.encrypt(fk, b"um documento importante")
    blocks = chunker.split(b"um documento importante", crypto.generate_file_key())
    fm = vault.record_version(path="x.txt", fk=fk, kek=kek_velha, device="a",
                              content_hash="blake3:h", size=23, blocks=blocks)
    assert vault.file_key(fm, kek_velha) == fk

    vault.rotate_kek(kek_velha, kek_nova, new_epoch=2)

    assert vault.kek_epoch == 2
    assert fm.wrapped_by_epoch == 2
    # com a KEK nova ainda recuperamos a MESMA FK...
    fk_apos = vault.file_key(fm, kek_nova)
    assert fk_apos == fk
    # ...e ela decifra o conteúdo que nunca foi recriptografado
    assert crypto.decrypt(fk_apos, conteudo_cifrado) == b"um documento importante"


def test_rotacao_epoch_invalido(vault, keyring):
    _, kr = keyring
    with pytest.raises(ValueError):
        vault.rotate_kek(kr.kek, crypto.generate_file_key(), new_epoch=1)


def test_rotacao_e_atomica_em_falha(vault, keyring):
    """Se um wrapped_fk estiver corrompido, a rotação não deixa o manifesto meio-feito."""
    from cryptography.exceptions import InvalidTag

    _, kr = keyring
    fm_ok, _ = _record(vault, kr.kek, path="a.txt")
    fm_ruim, _ = _record(vault, kr.kek, path="b.txt")
    wrapped_ok_antes = fm_ok.wrapped_fk
    fm_ruim.wrapped_fk = "AAAAAAAAAAAAAAAAAAAAAAAAAAAA"   # base64 que não desembrulha

    with pytest.raises(InvalidTag):
        vault.rotate_kek(kr.kek, crypto.generate_file_key(), new_epoch=2)

    assert vault.kek_epoch == 1                    # nada mudou
    assert fm_ok.wrapped_fk == wrapped_ok_antes    # o arquivo "bom" não foi tocado
    assert fm_ok.wrapped_by_epoch == 1
