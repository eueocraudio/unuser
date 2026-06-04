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

Requer **Python ≥ 3.11** (o código usa sintaxe de união `X | None`). Deps de runtime:
`cryptography`, `argon2-cffi`, `blake3`; de dev só `pytest`. Não há linter/formatter
configurado no `pyproject.toml`.

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

## Estrutura do código (layout `src/`)

O código vive em **`src/`**, dividido em três pacotes de topo (sem namespace `unuser`):

- **`src/client/`** — o lado que cifra/decifra: `crypto.py`, `chunker.py`, `index.py`,
  `manifest.py`, `transport.py`, `scanner.py`, `sync.py`, `actions.py`, `config.py`,
  `cli.py`, `gui.py`. Imports: `from client.crypto import ...`.
- **`src/server/`** — o cofre-cego (nunca decifra): `storage.py`, `http_server.py`.
  Imports: `from server.storage import ...`.
- **`src/common/`** — compartilhado pelos dois lados: `tls.py` (contextos mTLS).
  Imports: `from common.tls import ...`.

Os testes encontram os pacotes via `pythonpath = ["src"]` (em `pyproject.toml`); para
rodar snippets soltos com `.venv/bin/python`, use `PYTHONPATH=src`. (Há um
`src/__init__.py` herdado com `__version__`, mas ele **não** forma um pacote importável
no layout atual.)

## Arquitetura de criptografia (`src/client/crypto.py`)

Hierarquia de chaves (criptografia em **envelope**):

- `derive_root_key(passphrase, keyfile, salt)` — **ambos** passphrase e keyfile são
  obrigatórios: Argon2id(passphrase) combinado com o keyfile via HKDF. Mudar
  qualquer um dos dois muda a `root_key`.
- `derive_keyring(root_key)` → `Keyring(kek, name_key, manifest_key, block_id_key)`,
  separadas por rótulo de domínio no HKDF.
- `unlock(passphrase, keyfile, salt)` é o atalho de entrada: encadeia
  `derive_root_key` + `derive_keyring` e devolve o `Keyring` direto.
- **FK por arquivo:** cada arquivo é cifrado com uma chave aleatória (`generate_file_key`);
  a FK fica guardada no manifesto **embrulhada** pela KEK (`wrap_file_key`/`unwrap_file_key`).
  Isso permite **rotação da chave-mestra sem recriptografar o conteúdo** — só re-embrulha
  as FKs (ver teste `test_rotacao_reenvelopa_sem_tocar_conteudo`).
- `encrypt`/`decrypt` — AES-256-GCM, formato `nonce || ct || tag`, com AAD opcional.
- `block_id` — HMAC-SHA256 com chave (deduplicação dentro do arquivo sem vazar conteúdo).
- `content_hash` — BLAKE3 (detecção de modificação / integridade).

Regra inviolável: **passphrase e keyfile nunca são gravados** em config — só geram a KEK
em memória. Não introduza primitivas de cripto "caseiras"; use só as bibliotecas já em uso.

## Chunking (`src/client/chunker.py`)

Content-defined chunking via **Gear hash determinístico** (`_GEAR` derivado por SHA-256 de
rótulo fixo — **não** aleatório, senão as fronteiras diferiam entre máquinas e a dedup
quebraria). `chunk_bytes` recorta; `split(data, block_id_key)` já devolve `Block`s com
`block_id`. Editar um trecho só muda os blocos próximos (ver
`test_edit_local_preserva_a_maioria_dos_blocos`). Parâmetros padrão: min 4 KiB / avg 16 KiB
/ max 64 KiB. MVP lê o arquivo inteiro em memória (streaming fica para depois).

## Índice local (`src/client/index.py`)

`Index` (SQLite, context manager) guarda o estado conhecido por arquivo (`files`),
a lista ordenada de blocos (`file_blocks`, com cascade no delete), o catálogo `blocks`
e um `meta` chave-valor. É a visão **local e descartável** — o histórico canônico de
versões vive no manifesto/servidor. `set_blocks` aceita `BlockRef`, tuplas ou
`chunker.Block`. FK cascade exige `PRAGMA foreign_keys = ON` (já ligado no `__init__`).

## Manifesto (`src/client/manifest.py`)

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

### Versionamento de FORMATO e migração na leitura

Três "versões" convivem e **não se confundem**: `format_version` (esquema serializado),
`vault_version` (contador de mutação p/ CAS) e o histórico por arquivo em `FileVersion`.

Para o esquema, o leitor entende **múltiplas versões**: `migrate(raw, target=…)` eleva um
dict cru em cadeia (v1→v2→…→atual) e é o ponto único chamado por `from_dict` (logo
`loads`/`open_sealed` herdam a migração). Regras:

- Versão lida **> atual** → `UnsupportedManifestVersion` (arquivo de software mais novo).
- Versão lida **< atual** → aplica `_MIGRATIONS[v]` para cada `v`; passo faltando →
  `MigrationError`. Manifesto **sem** o campo = legado v1.
- `migrate` não muta a entrada (deepcopy) e carimba `format_version` a cada passo.

**Para introduzir um formato v2:** escreva a função `v1→v2(raw)->raw` (só reformata os
dados; o carimbo é automático), registre `_MIGRATIONS[1] = …` e suba
`CURRENT_FORMAT_VERSION` para 2. Cobertura em `tests/test_manifest_versioning.py`.

## Motor de sincronização (`scanner.py`, `sync.py`, `actions.py`)

Implementa o fluxo do cliente (§7): leitura/diagnóstico (`scanner`+`sync`) e as ações que
mutam (`actions`).

- `scanner.py` — varre as raízes (recursivas) + itens avulsos, aplica o `.unuserignore`
  (`IgnoreRules`, subconjunto estilo gitignore com defaults de segurança da §9.3) e
  devolve `vault_path -> ScannedFile` (com `content_hash`). **Vault path** = caminho
  relativo à raiz, prefixado pelo **nome** da raiz (`/home/ana/Documentos/x` →
  `Documentos/x`) — estável entre máquinas com `$HOME` diferentes (premissa confirmada).
- `sync.py` — **diff de 3 vias** por arquivo: `local` (scanner) × `base` (hash da última
  sync no `index`) × `servidor` (hash da versão atual no manifesto). A `base` é o
  ancestral comum que diz *quem* mudou. `classify(local, base, remote)` é **pura** e
  concentra toda a regra dos 6 status da §7.1 (`FileStatus`); `remote` pode ser `DELETED`
  (tombstone). `SyncEngine.status(scanned, manifest)` percorre a união dos caminhos. É o
  que finalmente usa o `index.py`.
- `actions.py` — as ações do §7.2 via `VaultSession` (liga `VaultClient`+`Keyring`+`Index`
  +`PathResolver`): `send`/`send_many` (cifra blocos, sobe só os que faltam por dedup via
  `index.has_block`, e `record_version` no manifesto — por §8 **todo envio versiona**,
  então "Substituir" e "Versionar" são a mesma operação de armazenamento), `receive`
  (baixa, **verifica `content_hash`**, grava e atualiza a base) e `delete` (tombstone §8 +
  remove local). Push do manifesto é **CAS**: `ConflictError` se o servidor avançou — quem
  chama recarrega e repete. `PathResolver` traduz vault path ↔ caminho local
  (`raiz.parent / vault_path`). A base local só é gravada **após** o commit ser aceito.

## Configuração e CLI (`src/client/config.py`, `src/client/cli.py`)

- `config.py` — carrega a §9: `ConnConfig.from_env` (`~/.env`, `CHAVE=VALOR`, remove
  comentário inline) → `make_client()` (modo `direct`; `tor` ainda levanta
  `NotImplementedError`, Fase 5); `ContentConfig.from_json` (`dirs.json`) →
  `path_resolver()` e `scan()`.
- `cli.py` — executável `unuser` (entry point em `pyproject` `[project.scripts]`).
  Subcomandos `status`/`send`/`receive`/`delete` sobre o `VaultSession`. Passphrase via
  `getpass` ou `UNUSER_PASSPHRASE`; keyfile via `--keyfile`/`UNUSER_KEYFILE`. **Salt do
  Argon2** (não-secreto) é guardado no índice (`meta`): cofre novo gera e persiste; cofre
  existente numa máquina nova exige o salt copiado junto (limitação conhecida do MVP — a
  alternativa seria publicar o salt em claro no servidor). E2E em `tests/test_cli.py`.

## Servidor cofre-cego (`src/server/`)

- `storage.py` — `BlindStorage` em disco: blobs (validados por regex `^b:[0-9a-f]{64}$`
  — anti path-traversal), manifesto com **CAS** (`put_manifest(expected, new, blob)` →
  `ConflictError` se a versão atual ≠ esperada; lock para concorrência). Nunca decifra nada.
- `http_server.py` — API stdlib (`ThreadingHTTPServer`): `/healthz`, `GET/PUT /manifest`
  (versão via headers `X-Unuser-*`), `HEAD/GET/PUT /blob/<id>`. `make_server(..., ssl_context=)`
  habilita mTLS. `port=0` = porta efêmera (testes).
- `cli.py` — executável **`unuserd`** (entry point `[project.scripts]`): `build_server(args)`
  monta `BlindStorage`+`make_server` (mTLS se `--tls-cert/key/allow`), `main` trata
  SIGTERM (stop do systemd) e faz `serve_forever`. Importa `common.tls` só com mTLS.
- `src/common/tls.py` — geração de certs EC autoassinados, `cert_fingerprint`, e contextos
  mTLS. Allowlist = arquivo PEM concatenando os certs de cliente confiáveis
  (`write_allowlist`); o servidor faz `verify_mode=CERT_REQUIRED`.
- `src/client/transport.py` — `VaultClient` (http.client) que fala a API; aceita
  `ssl_context` para mTLS e `socks_proxy=(host,port)` para tunelar pelo SOCKS5 do Tor
  (`socks5_connect`, CONNECT por ATYP=domínio p/ o Tor resolver o `.onion`). 409 → `ConflictError`.

**Tor** é operacional (ver `doc/operacao-tor.md`): o `unuserd` só escuta TLS em
localhost; o daemon `tor` publica o Onion Service; o cliente conecta ao `.onion` pelo
SOCKS5 (modo `tor` em `config.make_client`), com o mTLS por dentro do túnel.

## GUI (`src/client/gui.py`)

Janela PySide6 estilo XP Explorer/Luna (§5), via QSS (`LUNA_QSS`) — sem assets
proprietários. `MainWindow` recebe um **controller** desacoplado da rede
(`status()`/`send`/`receive`/`delete(paths)`/`connection_label()`), exibe a árvore com as
6 cores de status (`STATUS_COLORS`), o painel de tarefas à esquerda e as ações na barra +
menu de contexto. As operações rodam **fora da thread da UI** (`_Worker`/`QThreadPool` de
1 thread → serializado; resultado entregue por sinal na thread da UI), com estado
"ocupado" (cursor + ações desabilitadas) — não trava em rede lenta via Tor. O índice da
GUI usa `check_same_thread=False` (acesso serializado pelo pool). Lançada por `unuser gui`
(import tardio do PySide6, dep opcional `gui`). Testável sem display:
`tests/test_gui.py` usa `QT_QPA_PLATFORM=offscreen` + controller falso
(`async_run=False` p/ determinismo; um teste exercita o caminho threaded).

## Empacotamento (`packaging/`)

Fase 6: o servidor vira `.deb` + systemd. `packaging/build-deb.sh` monta um pacote
binário do `unuserd` **sem root** (`dpkg-deb --root-owner-group`) — só `server`+`common`,
`Depends: python3, python3-cryptography` — em `dist/unuserd_<ver>_all.deb`.
`packaging/systemd/unuserd.service` (roda como usuário `unuser`, endurecido) +
`packaging/default/unuserd` (env com `UNUSERD_ARGS`). O cliente (GUI/CLI, deps pesadas)
instala-se por `pip install -e ".[gui]"` — ver `packaging/README.md`.

## Roadmap

Fases 1–6 ✓ (cripto · chunker+índice · manifesto · servidor cofre-cego+mTLS · motor de
sync+CLI · GUI PySide6 (threaded) + SOCKS5/Tor · empacotamento `unuserd` .deb/systemd).
Pendências de refino: salt cross-máquina, retry no `ConflictError`, e um `.deb` para o
cliente. Acesso remoto é **Tor** (sem VPN).
