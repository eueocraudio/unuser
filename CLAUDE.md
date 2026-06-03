# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Visão geral

**unuser** (UniqueUser) sincroniza documentos entre máquinas Debian via um servidor
central **cofre-cego** (zero-knowledge): o servidor só guarda blobs e manifestos
cifrados, sem nunca ver conteúdo, nomes ou chaves. O cliente é um app gráfico
(PySide6, visual estilo Windows XP Explorer) e a sincronização é **manual**,
controlada por status/ações na interface.

A especificação completa e canônica está em **`doc/especificacao-unuser.html`**
(fonte editável) — leia-a antes de mexer na arquitetura. O PDF é gerado a partir dela.

## Ambiente (peculiaridade importante)

A máquina **não tem `ensurepip` nem `python3-venv`**, e `sudo` exige senha. Por isso
o venv foi montado **sem sudo**, com pip via `get-pip.py`:

```bash
python3 -m venv --without-pip .venv
curl -sS -o /tmp/get-pip.py https://bootstrap.pypa.io/get-pip.py
.venv/bin/python /tmp/get-pip.py
.venv/bin/python -m pip install -e ".[dev]"
```

Não tente `python3 -m venv .venv` puro (falha por falta de ensurepip).

## Comandos

```bash
# Rodar todos os testes
.venv/bin/python -m pytest

# Um teste específico
.venv/bin/python -m pytest tests/test_crypto.py::test_wrap_unwrap_fk -v

# Regenerar o PDF da especificação a partir do HTML (não há pandoc/LaTeX; usa LibreOffice)
soffice --headless -env:UserInstallation=file:///tmp/lo_unuser_profile \
  --convert-to pdf --outdir doc doc/especificacao-unuser.html
```

## Arquitetura de criptografia (`unuser/crypto.py`)

Hierarquia de chaves (criptografia em **envelope**):

- `derive_root_key(passphrase, keyfile, salt)` — **ambos** passphrase e keyfile são
  obrigatórios: Argon2id(passphrase) combinado com o keyfile via HKDF. Mudar
  qualquer um dos dois muda a `root_key`.
- `derive_keyring(root_key)` → `Keyring(kek, name_key, manifest_key, block_id_key)`,
  separadas por rótulo de domínio no HKDF.
- **FK por arquivo:** cada arquivo é cifrado com uma chave aleatória (`generate_file_key`);
  a FK fica guardada no manifesto **embrulhada** pela KEK (`wrap_file_key`/`unwrap_file_key`).
  Isso permite **rotação da chave-mestra sem recriptografar o conteúdo** — só re-embrulha
  as FKs (ver teste `test_rotacao_reenvelopa_sem_tocar_conteudo`).
- `encrypt`/`decrypt` — AES-256-GCM, formato `nonce || ct || tag`, com AAD opcional.
- `block_id` — HMAC-SHA256 com chave (deduplicação dentro do arquivo sem vazar conteúdo).
- `content_hash` — BLAKE3 (detecção de modificação / integridade).

Regra inviolável: **passphrase e keyfile nunca são gravados** em config — só geram a KEK
em memória. Não introduza primitivas de cripto "caseiras"; use só as bibliotecas já em uso.

## Chunking (`unuser/chunker.py`)

Content-defined chunking via **Gear hash determinístico** (`_GEAR` derivado por SHA-256 de
rótulo fixo — **não** aleatório, senão as fronteiras diferiam entre máquinas e a dedup
quebraria). `chunk_bytes` recorta; `split(data, block_id_key)` já devolve `Block`s com
`block_id`. Editar um trecho só muda os blocos próximos (ver
`test_edit_local_preserva_a_maioria_dos_blocos`). Parâmetros padrão: min 4 KiB / avg 16 KiB
/ max 64 KiB. MVP lê o arquivo inteiro em memória (streaming fica para depois).

## Índice local (`unuser/index.py`)

`Index` (SQLite, context manager) guarda o estado conhecido por arquivo (`files`),
a lista ordenada de blocos (`file_blocks`, com cascade no delete), o catálogo `blocks`
e um `meta` chave-valor. É a visão **local e descartável** — o histórico canônico de
versões vive no manifesto/servidor. `set_blocks` aceita `BlockRef`, tuplas ou
`chunker.Block`. FK cascade exige `PRAGMA foreign_keys = ON` (já ligado no `__init__`).

## Manifesto (`unuser/manifest.py`)

Modelo da seção 4.4: `VaultManifest` (cabeçalho global + índice `files`) →
`FileManifest` (por arquivo: `wrapped_fk`, `wrapped_by_epoch`, `versions`) →
`FileVersion` (`ts` UTC p/ desempate, `hash`, `size`, `blocks`, `device`, `deleted`).
Pontos-chave:

- **FK é por arquivo, não por versão** — `record_version` só embrulha a FK quando o
  arquivo é novo; versões seguintes reusam a mesma `wrapped_fk`.
- `rotate_kek(old, new, epoch)` re-embrulha todas as FKs **sem tocar no conteúdo**
  (ver `test_rotacao_reembrulha_sem_tocar_conteudo`).
- `mark_deleted` adiciona **tombstone** (apaga mantendo histórico).
- `vault_version` incrementa a cada mutação (base do CAS no servidor, Fase 4).
- `dumps` é JSON canônico (sort_keys); `seal`/`open_sealed` cifram com a `manifest_key`.
  `ts` é UTC `YYYY-MM-DD HH:MM:SS` (largura fixa → ordena lexicograficamente).

## Servidor cofre-cego (`unuser/server/`)

- `storage.py` — `BlindStorage` em disco: blobs (validados por regex `^b:[0-9a-f]{64}$`
  — anti path-traversal), manifesto com **CAS** (`put_manifest(expected, new, blob)` →
  `ConflictError` se a versão atual ≠ esperada; lock para concorrência). Nunca decifra nada.
- `http_server.py` — API stdlib (`ThreadingHTTPServer`): `/healthz`, `GET/PUT /manifest`
  (versão via headers `X-Unuser-*`), `HEAD/GET/PUT /blob/<id>`. `make_server(..., ssl_context=)`
  habilita mTLS. `port=0` = porta efêmera (testes).
- `unuser/tls.py` — geração de certs EC autoassinados, `cert_fingerprint`, e contextos
  mTLS. Allowlist = arquivo PEM concatenando os certs de cliente confiáveis
  (`write_allowlist`); o servidor faz `verify_mode=CERT_REQUIRED`.
- `unuser/client/transport.py` — `VaultClient` (http.client) que fala a API; aceita
  `ssl_context` para mTLS. Levanta `ConflictError` no 409.

**Tor** é operacional (ver `doc/operacao-tor.md`): o `unuserd` só escuta TLS em
localhost; o daemon `tor` publica o Onion Service. O tunelamento via SOCKS5 do cliente
fica para a Fase 5.

## Roadmap

Fases 1–4 ✓ concluídas (cripto · chunker+índice · manifesto · servidor cofre-cego +
mTLS). Próximas: 5) GUI PySide6 estilo XP (treeview, status, 4 ações, send/receive,
escolha IP/Tor + SOCKS) · 6) empacotamento (.deb/systemd). Acesso remoto é **Tor**
(sem VPN).
