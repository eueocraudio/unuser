"""Testes de segurança: provam as garantias zero-knowledge como propriedades adversariais.

Diferente dos testes unitários (que checam o comportamento de cada peça), aqui cada
teste afirma uma **promessa de segurança** do unuser na forma "o atacante NÃO consegue X":

* o servidor cofre-cego nunca tem plaintext, nomes ou chaves no disco;
* o ``block_id`` é com chave (HMAC) — não dá para reproduzi-lo nem inferir o conteúdo;
* o manifesto selado não revela caminhos nem conteúdo, e exige a chave certa;
* a FK só sai do envelope com a KEK certa;
* qualquer adulteração de bytes cifrados é detectada (AES-GCM);
* abrir o cofre exige os DOIS fatores (passphrase E keyfile);
* entradas hostis (path traversal) são rejeitadas já na borda HTTP.
"""

import threading
from contextlib import contextmanager

import pytest
from cryptography.exceptions import InvalidTag

from client import chunker, crypto, manifest
from client.transport import TransportError, VaultClient
from server.http_server import make_server
from server.storage import BlindStorage

# Marcadores reconhecíveis: se vazarem para o disco do servidor, o teste pega.
MARKER = b"MARCADOR-SECRETO-DO-USUARIO"
SECRET_PATH = "documentos/senhas-bancarias.txt"


@contextmanager
def running(storage):
    httpd = make_server(storage)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


@pytest.fixture
def keyring():
    """Keyring derivado dos dois fatores (passphrase + keyfile)."""
    salt = crypto.generate_salt()
    return crypto.unlock("senha-forte-do-usuario", b"keyfile-de-alta-entropia", salt)


def _client_push(client: VaultClient, keyring: crypto.Keyring,
                 files: dict[str, bytes]) -> manifest.VaultManifest:
    """Reproduz o fluxo do cliente: cifra → chunk → cifra blocos → empurra blobs +
    manifesto selado ao servidor. Devolve o VaultManifest local (em claro)."""
    vault = manifest.VaultManifest.new("vault-teste", crypto.generate_salt())
    for path, data in files.items():
        fk = crypto.generate_file_key()
        blocks = chunker.split(data, keyring.block_id_key)
        for b in blocks:
            client.put_blob(b.block_id, crypto.encrypt(fk, b.data))  # só ciphertext sai
        vault.record_version(
            path=path, fk=fk, kek=keyring.kek, device="dev-1",
            content_hash=crypto.content_hash(data), size=len(data), blocks=blocks,
        )
    client.put_manifest(0, vault.vault_version, manifest.seal(vault, keyring.manifest_key))
    return vault


def _all_server_bytes(root) -> bytes:
    """Concatena tudo que o servidor gravou no disco (blobs + manifesto + metadados)."""
    blob = b""
    for p in sorted(root.rglob("*")):
        if p.is_file():
            blob += p.read_bytes()
    return blob


# --- 1. servidor só armazena ciphertext -------------------------------------

def test_servidor_so_armazena_ciphertext(tmp_path, keyring):
    """E2E: depois de enviar um arquivo, NADA do plaintext (nem o caminho) aparece
    no disco do servidor — mas o blob baixado ainda decifra de volta ao original."""
    data = (MARKER + b" " + bytes(range(256))) * 300  # ~78 KiB, variado → vários blocos
    store = BlindStorage(tmp_path / "vault")
    with running(store) as port:
        c = VaultClient("127.0.0.1", port)
        vault = _client_push(c, keyring, {SECRET_PATH: data})

        on_disk = _all_server_bytes(tmp_path / "vault")
        assert MARKER not in on_disk                      # conteúdo não vazou
        assert SECRET_PATH.encode() not in on_disk        # o nome do arquivo não vazou
        assert b"senhas-bancarias" not in on_disk
        assert store.list_blobs(), "o servidor deveria ter guardado algum blob"

        # E mesmo assim o cliente recupera o original a partir do ciphertext.
        fm = vault.file_by_path(SECRET_PATH)
        fk = vault.file_key(fm, keyring.kek)
        montado = b"".join(
            crypto.decrypt(fk, c.get_blob(bid)) for bid in fm.current.blocks
        )
        assert montado == data


def test_manifesto_no_servidor_nao_revela_nomes(tmp_path, keyring):
    """O manifesto que o servidor guarda é opaco: nem o caminho nem 'vault-teste'
    legível aparecem — e sem a manifest_key não dá para abri-lo."""
    store = BlindStorage(tmp_path / "vault")
    with running(store) as port:
        c = VaultClient("127.0.0.1", port)
        _client_push(c, keyring, {SECRET_PATH: MARKER * 500})
        _, sealed = c.get_manifest()

    assert sealed
    assert SECRET_PATH.encode() not in sealed
    assert MARKER not in sealed
    with pytest.raises(InvalidTag):                       # chave errada não abre
        manifest.open_sealed(sealed, crypto.generate_file_key())


# --- 2. block_id é com chave -------------------------------------------------

def test_block_id_e_com_chave_e_nao_reproduzivel(keyring):
    """O mesmo conteúdo sob chaves diferentes gera ids diferentes; um atacante sem a
    block_id_key não consegue reproduzir o id (logo não confirma o que um blob contém)."""
    bloco = MARKER * 200
    id_legitimo = crypto.block_id(keyring.block_id_key, bloco)

    outra_chave = crypto.generate_file_key()
    assert crypto.block_id(outra_chave, bloco) != id_legitimo   # depende da chave

    # O atacante conhece o conteúdo mas não a chave: qualquer palpite falha.
    for _ in range(20):
        assert crypto.block_id(crypto.generate_file_key(), bloco) != id_legitimo


def test_block_id_estavel_entre_maquinas(keyring):
    """Com a MESMA chave o id é determinístico (condição da dedup entre clientes)."""
    bloco = MARKER * 123
    assert crypto.block_id(keyring.block_id_key, bloco) == \
        crypto.block_id(keyring.block_id_key, bloco)


# --- 3. envelope da FK -------------------------------------------------------

def test_fk_irrecuperavel_sem_a_kek(keyring):
    """A FK embrulhada não contém a FK crua e só sai com a KEK correta."""
    fk = crypto.generate_file_key()
    wrapped = crypto.wrap_file_key(keyring.kek, fk)

    assert fk not in wrapped                              # não está em claro no envelope
    assert crypto.unwrap_file_key(keyring.kek, wrapped) == fk

    kek_errada = crypto.generate_file_key()
    with pytest.raises(InvalidTag):
        crypto.unwrap_file_key(kek_errada, wrapped)


def test_rotacao_de_kek_nao_expoe_fk_antiga(keyring):
    """Após rotacionar a KEK, a KEK antiga não abre mais o envelope, mas o conteúdo
    (FK) é o mesmo — prova que rotacionar não recriptografa o conteúdo."""
    vault = manifest.VaultManifest.new("v", crypto.generate_salt())
    fk = crypto.generate_file_key()
    blocks = chunker.split(MARKER * 400, keyring.block_id_key)
    vault.record_version(path="a.txt", fk=fk, kek=keyring.kek, device="d",
                         content_hash="blake3:x", size=1, blocks=blocks)

    nova_kek = crypto.generate_file_key()
    vault.rotate_kek(keyring.kek, nova_kek, new_epoch=vault.kek_epoch + 1)
    fm = vault.file_by_path("a.txt")

    assert vault.file_key(fm, nova_kek) == fk             # mesma FK, novo envelope
    with pytest.raises(InvalidTag):
        vault.file_key(fm, keyring.kek)                   # KEK antiga não abre mais


# --- 4. detecção de adulteração ---------------------------------------------

def test_adulteracao_de_bloco_e_detectada(keyring):
    """Virar um único bit do ciphertext de um bloco quebra a verificação do GCM."""
    fk = crypto.generate_file_key()
    blob = bytearray(crypto.encrypt(fk, MARKER * 50))
    blob[-1] ^= 0x01
    with pytest.raises(InvalidTag):
        crypto.decrypt(fk, bytes(blob))


def test_adulteracao_de_manifesto_e_detectada(keyring):
    """O servidor (ou um intermediário) não pode mexer no manifesto sem ser pego."""
    vault = manifest.VaultManifest.new("v", crypto.generate_salt())
    sealed = bytearray(manifest.seal(vault, keyring.manifest_key))
    sealed[len(sealed) // 2] ^= 0xFF
    with pytest.raises(InvalidTag):
        manifest.open_sealed(bytes(sealed), keyring.manifest_key)


# --- 5. dois fatores (passphrase + keyfile) ---------------------------------

def test_abrir_cofre_exige_os_dois_fatores():
    """Selado com (senha, keyfile A): nem a senha sozinha (keyfile errado) nem o
    keyfile sozinho (senha errada) abrem o manifesto."""
    salt = crypto.generate_salt()
    senha, keyfile = "senha-correta", b"keyfile-correto-de-alta-entropia"
    kr = crypto.unlock(senha, keyfile, salt)

    vault = manifest.VaultManifest.new("v", salt)
    sealed = manifest.seal(vault, kr.manifest_key)

    # senha certa, keyfile errado -> manifest_key diferente -> não abre
    kr_kf_errado = crypto.unlock(senha, b"keyfile-do-atacante", salt)
    with pytest.raises(InvalidTag):
        manifest.open_sealed(sealed, kr_kf_errado.manifest_key)

    # keyfile certo, senha errada -> idem
    kr_senha_errada = crypto.unlock("senha-chutada", keyfile, salt)
    with pytest.raises(InvalidTag):
        manifest.open_sealed(sealed, kr_senha_errada.manifest_key)

    # os dois certos -> abre
    assert manifest.open_sealed(sealed, kr.manifest_key).vault_id == "v"


# --- 6. borda HTTP rejeita entradas hostis ----------------------------------

def test_path_traversal_no_http_e_rejeitado(tmp_path):
    """Um block_id forjado com '../' é recusado (400) e nada é gravado fora dos blobs."""
    store = BlindStorage(tmp_path / "vault")
    with running(store) as port:
        c = VaultClient("127.0.0.1", port)
        for ruim in ("../../etc/pwn", "b:" + "../" * 10, "b:NAOHEX", "b:" + "ab" * 31):
            with pytest.raises(TransportError):
                c.put_blob(ruim, b"carga-maliciosa")

    assert store.list_blobs() == []                       # nenhum blob escapou
    assert not (tmp_path / "vault" / "pwn").exists()
    assert b"carga-maliciosa" not in _all_server_bytes(tmp_path)
