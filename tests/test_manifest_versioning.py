"""Testes do versionamento de FORMATO do manifesto e da migração na leitura.

Garante que o leitor (`loads`/`open_sealed`/`from_dict`) entende manifestos de esquemas
antigos, migrando-os até o formato atual, e recusa com erro claro os de versão futura.
"""

import json

import pytest

from client import crypto, manifest
from client.manifest import (
    CURRENT_FORMAT_VERSION, MigrationError, UnsupportedManifestVersion, VaultManifest,
)


def _raw(**over) -> dict:
    """Dict cru mínimo de um manifesto (na versão atual, salvo override)."""
    d = {
        "format_version": CURRENT_FORMAT_VERSION,
        "vault_id": "v:abc", "argon2_salt": "AAAA", "kek_epoch": 1,
        "vault_version": 0, "files": {},
    }
    d.update(over)
    return d


# --- caminho comum: versão atual -------------------------------------------

def test_versao_atual_roundtrip_sem_migracao():
    v = VaultManifest.new("v:abc", crypto.generate_salt())
    assert VaultManifest.from_dict(v.to_dict()).to_dict() == v.to_dict()


def test_dumps_carrega_format_version():
    v = VaultManifest.new("v:abc", crypto.generate_salt())
    assert json.loads(manifest.dumps(v))["format_version"] == CURRENT_FORMAT_VERSION


def test_manifesto_legado_sem_campo_assume_v1():
    """Manifestos pré-versionamento (sem `format_version`) são tratados como v1."""
    d = _raw()
    del d["format_version"]
    assert manifest._detect_version(d) == 1
    assert VaultManifest.from_dict(d).vault_id == "v:abc"  # carrega sem erro


# --- versão futura é recusada ----------------------------------------------

def test_versao_futura_e_recusada():
    futuro = _raw(format_version=CURRENT_FORMAT_VERSION + 1)
    with pytest.raises(UnsupportedManifestVersion) as ei:
        VaultManifest.from_dict(futuro)
    assert ei.value.found == CURRENT_FORMAT_VERSION + 1
    assert ei.value.supported == CURRENT_FORMAT_VERSION


def test_open_sealed_de_versao_futura_e_recusado():
    kr = crypto.unlock("s", b"kf", crypto.generate_salt(),
                       time_cost=1, memory_cost=8 * 1024, parallelism=1)
    blob = crypto.encrypt(kr.manifest_key,
                          json.dumps(_raw(format_version=99)).encode(),
                          aad=b"unuser:v1:manifest")
    with pytest.raises(UnsupportedManifestVersion):
        manifest.open_sealed(blob, kr.manifest_key)


# --- a engine de migração entende MÚLTIPLAS versões em cadeia ----------------
# Exercita a função real `migrate` com migrações de exemplo (v1->v2->v3), provando que,
# quando novos formatos forem registrados, o leitor sobe o dado passo a passo até o atual.

def test_migracao_em_cadeia_v1_para_v3():
    def v1_para_v2(raw: dict) -> dict:
        raw["apelido"] = raw["vault_id"].upper()      # campo novo no v2
        return raw

    def v2_para_v3(raw: dict) -> dict:
        raw["kek_epoch"] = raw["kek_epoch"] + 100      # reforma um campo no v3
        return raw

    antigo = _raw(format_version=1, vault_id="v:abc", kek_epoch=1)
    migrado = manifest.migrate(antigo, target=3,
                               migrations={1: v1_para_v2, 2: v2_para_v3})

    assert migrado["format_version"] == 3              # carimbo final
    assert migrado["apelido"] == "V:ABC"               # passo v1->v2 aplicado
    assert migrado["kek_epoch"] == 101                 # passo v2->v3 aplicado
    assert antigo["format_version"] == 1               # entrada original intacta (cópia)


def test_migracao_para_a_propria_versao_e_noop():
    d = _raw(format_version=2)
    assert manifest.migrate(d, target=2, migrations={}) == d


def test_falta_de_passo_de_migracao_falha():
    with pytest.raises(MigrationError):
        manifest.migrate(_raw(format_version=1), target=2, migrations={})  # buraco v1->v2


def test_migracao_nao_muta_a_entrada():
    antigo = _raw(format_version=1)
    manifest.migrate(antigo, target=2, migrations={1: lambda r: r})
    assert "format_version" in antigo and antigo["format_version"] == 1
