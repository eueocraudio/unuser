# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Visão geral

**unuser** (UniqueUser) sincroniza documentos entre máquinas Debian via um servidor
central **cofre-cego** (zero-knowledge): o servidor só guarda blobs e manifestos
cifrados, sem nunca ver conteúdo, nomes ou chaves. O cliente é um app gráfico
(PySide6, **layout Explorer em tema escuro**) e a sincronização é **manual**,
controlada por status/ações na interface.

Documentação:
- **`doc/especificacao-unuser.html`** — especificação técnica canônica (fonte editável;
  PDF gerado a partir dela). Leia antes de mexer na arquitetura.
- **`doc/manual-usuario-unuser.html`** — manual do usuário (instalação por `.deb` e do
  código, configuração, uso, GUI, Tor); PDF gerado a partir dele.

**Estado:** Fases 1–6 do roadmap concluídas + refinos (GUI com threading, retry
automático do CAS, chunking em streaming, salt cross-máquina, **block_id por arquivo**,
GUI repaginada: tema escuro + Explorer de duas áreas + ícones + adicionar arquivo/pastas +
persistência do estado da árvore). Cliente em **v1.0.1**. ~166 testes passando. Executáveis
`unuser` (cliente) e `unuserd` (servidor); `.deb` de ambos em `packaging/`; scripts de
conveniência em `src/server/run.sh`, `src/client/run.sh`, `src/client/run-gui.sh`.

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
`cryptography`, `argon2-cffi`, `blake3`; a GUI usa o extra `gui` (`PySide6-Essentials`,
instale com `pip install -e ".[gui]"`); de dev só `pytest`. Não há linter/formatter
configurado no `pyproject.toml`.

## Comandos

```bash
# Rodar todos os testes
.venv/bin/python -m pytest

# Um teste específico
.venv/bin/python -m pytest tests/test_crypto.py::test_wrap_unwrap_fk -v

# Regenerar os PDFs a partir do HTML (não há pandoc/LaTeX; usa LibreOffice)
soffice --headless -env:UserInstallation=file:///tmp/lo_unuser_profile \
  --convert-to pdf --outdir doc doc/especificacao-unuser.html
soffice --headless -env:UserInstallation=file:///tmp/lo_unuser_profile \
  --convert-to pdf --outdir doc doc/manual-usuario-unuser.html
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
- `block_id` — HMAC-SHA256 com chave (deduplicação **dentro do arquivo** sem vazar conteúdo).
  A chave do HMAC é **por arquivo**: `derive_block_id_key(block_id_key, fk)` (HKDF, FK como
  IKM). Assim blocos idênticos em arquivos diferentes (FKs diferentes) recebem ids
  diferentes — senão colidiriam no mesmo blob (endereçado por conteúdo), cifrado com a FK de
  só um deles, deixando o outro **irrecuperável**. Dentro do mesmo arquivo (mesma FK) a dedup
  intra-arquivo é preservada. Quem usa: `actions._upload_and_record`. Ver
  `tests/test_actions.py::test_blocos_iguais_em_arquivos_diferentes_nao_corrompem`.
- `content_hash` — BLAKE3 (detecção de modificação / integridade).

Regra inviolável: **passphrase e keyfile nunca são gravados** em config — só geram a KEK
em memória. Não introduza primitivas de cripto "caseiras"; use só as bibliotecas já em uso.

## Chunking (`src/client/chunker.py`)

Content-defined chunking via **Gear hash determinístico** (`_GEAR` derivado por SHA-256 de
rótulo fixo — **não** aleatório, senão as fronteiras diferiam entre máquinas e a dedup
quebraria). `chunk_bytes` recorta; `split(data, block_id_key)` já devolve `Block`s com
`block_id`. Editar um trecho só muda os blocos próximos (ver
`test_edit_local_preserva_a_maioria_dos_blocos`). Parâmetros padrão: min 4 KiB / avg 16 KiB
/ max 64 KiB.

**Streaming:** `chunk_stream(reader)` / `split_stream(reader, key)` / `split_file(path,
key)` chunkam sem carregar o arquivo inteiro (janela ≤ ~max_size). Produzem **exatamente**
as mesmas fronteiras que a versão em memória — testado byte-a-byte
(`test_chunk_stream_identico_ao_chunk_bytes`), inviolável p/ a dedup. `chunk_file` é
streaming. Quem usa: `actions._upload_and_record` (cifra/envia 1 bloco por vez, guarda só
`BlockRef`s e hasheia incremental via `crypto.content_hasher`) e `scanner` (hash por
partes). Assim nem o envio nem o scan seguram o arquivo todo em memória.

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
- **Formato "wire" (o que vai ao servidor):** `pack(vault, manifest_key)` =
  `{"argon2_salt": <claro>, "sealed": <selado>}`. O salt do Argon2 viaja **em claro**
  (parâmetro público de KDF, não-secreto) para uma máquina nova derivar as chaves sem
  cópia manual: `peek_salt(wire)` lê o salt **sem chave**; `unpack(wire, key)` abre o
  selado. Conteúdo/nomes/chaves seguem só na parte selada (servidor continua cego para
  eles). Adulterar o salt em claro só causa falha ao abrir (DoS), não vaza nada.

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
  remove local). Push do manifesto é **CAS** com **retry automático** (`_apply_with_retry`:
  em `ConflictError`, recarrega o manifesto fresco e **reaplica** a mutação, até
  `max_retries`=5; só desiste levantando `ConflictError` no fim). `PathResolver` traduz
  vault path ↔ caminho local (`raiz.parent / vault_path`). A base local só é gravada
  **após** o commit ser aceito.

## Configuração e CLI (`src/client/config.py`, `src/client/cli.py`)

- `config.py` — carrega a §9: `ConnConfig.from_env` (`~/.env`, `CHAVE=VALOR`, remove
  comentário inline) → `make_client()` (modo `direct` ou `tor` — este via SOCKS5, Fase 5
  pronta); `ContentConfig.from_json` (`dirs.json`) → `path_resolver()` e `scan()`.
  `ContentConfig` também **muta e persiste** o `dirs.json` (lembra a `source`): `save`,
  `add_dir`/`add_item`/`remove_dir`/`remove_item` (idempotentes) e
  `resolve_or_register(local, home)` — mapeia um caminho local → vault path, registrando a
  **pasta** do arquivo como raiz se ele estiver fora das raízes mas **dentro de `~/`** (ou
  como **item avulso** se estiver direto em `~/`, para nunca sincronizar a home inteira);
  fora de `~/` → `ValueError`. É o que o botão "Adicionar arquivo" da GUI usa.
- `cli.py` — executável `unuser` (entry point em `pyproject` `[project.scripts]`).
  Subcomandos `status`/`send`/`receive`/`delete`/`gui` sobre o `VaultSession`. Passphrase
  via `getpass` ou `UNUSER_PASSPHRASE`; keyfile via `--keyfile`/`UNUSER_KEYFILE`. **Salt do
  Argon2**: `_unlock` lê o salt do **manifesto publicado** (`peek_salt`, em claro) — uma
  máquina nova abre o cofre **sem cópia manual**; cofre novo gera o salt (cacheado no
  índice `meta`). O `_Controller` do `gui` expõe, além de status/send/receive/delete:
  `add` (usa `resolve_or_register`), `sync_folders`/`add_root`/`remove_root`/`remove_item`
  (gerência de pastas) e `ui_state`/`save_ui_state` (estado da GUI em
  **`~/.local/data/unuser/gui-state.json`** — `_DATA_DIR`, criado recursivamente).
  E2E single-machine em `tests/test_cli.py`; cross-máquina em
  `tests/test_actions.py::test_maquina_nova_abre_cofre_pelo_salt_publicado`.

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

Janela PySide6 com **layout Explorer de duas áreas em tema escuro** (§5), via QSS
(`DARK_QSS`) — sem assets proprietários. `MainWindow` recebe um **controller** desacoplado
da rede; layout:

- **Esquerda:** `folder_tree` — TreeView de **pastas** (hierárquica, raiz "Cofre"),
  construída a partir dos vault paths conhecidos.
- **Direita:** `tree` — **lista** dos arquivos **diretamente** na pasta selecionada (NÃO
  recursivo; subpastas ficam na árvore), cada um com a sua cor de status (`STATUS_COLORS`,
  6 tons vivos p/ fundo escuro). Exibe o basename; o `FileState` (com vault path absoluto)
  fica no `UserRole`.
- **Ícones:** pastas e arquivos usam os ícones padrão do Qt (`QStyle.SP_DirIcon`/
  `SP_FileIcon`, criados em `MainWindow.__init__` como `_icon_dir`/`_icon_file`) — sem assets
  próprios; aplicados na árvore, na lista e no `SyncFoldersDialog`.

Barra de ferramentas: Atualizar/Enviar/Receber/Apagar, **Adicionar arquivo** (seletor →
`controller.add` via `resolve_or_register`, auto-registra a pasta) e **Pastas…**
(`SyncFoldersDialog`: lista/adiciona/remove raízes e itens avulsos). Ações também no menu
de contexto da lista.

- **Estado da árvore persistente:** expansão por nó + pasta selecionada são preservadas
  entre reconstruções **e entre sessões** — `_load_ui_state`/`_save_ui_state` via
  `controller.ui_state`/`save_ui_state` (arquivo em `~/.local/data/unuser/gui-state.json`).
  Sinais `itemExpanded`/`itemCollapsed`/`itemSelectionChanged` salvam; o rebuild restaura
  (1ª vez sem estado → expande tudo; pastas novas nascem colapsadas).
- **Threading:** operações rodam **fora da thread da UI** (`_Worker`/`QThreadPool` de 1
  thread → serializado; resultado por sinal na thread da UI), com estado "ocupado" — não
  trava em rede lenta via Tor. O índice usa `check_same_thread=False` (acesso serializado
  pelo pool); o estado da GUI é gravado em **arquivo** (não no SQLite) para não tocar a
  conexão a partir da thread da UI.

Lançada por `unuser gui` (import tardio do PySide6, dep opcional `gui`) ou pelo
`src/client/run-gui.sh` (instala o extra `gui` + resolve `libxcb-cursor0` sem root).
Testável sem display: `tests/test_gui.py` usa `QT_QPA_PLATFORM=offscreen` + controller
falso (`async_run=False` p/ determinismo; um teste exercita o caminho threaded; outros
cobrem filtro por pasta, persistência de estado, adicionar arquivo e o diálogo de pastas).

## Empacotamento (`packaging/`)

Fase 6: dois `.deb` montados sem root (`dpkg-deb --root-owner-group`).
- **Servidor** (`build-deb.sh`): `unuserd`, só `server`+`common`, `Depends: python3,
  python3-cryptography`, `Architecture: all`, em `dist/unuserd_<ver>_all.deb`. +
  `systemd/unuserd.service` (usuário `unuser`, endurecido) + `default/unuserd`.
- **Cliente** (`build-deb-client.sh`): `unuser`, `client`+`common`, depende dos pacotes
  de sistema (`python3-cryptography`, `python3-argon2`; `Recommends:
  python3-pyside6.qtwidgets` p/ a GUI) e **embarca só o `blake3`** (não existe no apt) →
  `Architecture: amd64`, fixado ao python3.13, `dist/unuser_<ver>_amd64.deb`. + atalho
  `.desktop`. O build copia o blake3 do venv (`pip install -e ".[dev]"` antes).
Ver `packaging/README.md`. Os `.deb` foram verificados rodando com o python3 do **sistema**
(fora do venv).

## Scripts de execução (`src/server/`, `src/client/`)

Conveniência para rodar sem decorar flags (acham o binário do `.venv` ou do PATH):
- `src/server/run.sh` — sobe o `unuserd` (defaults de storage/host/porta; mTLS via
  `UNUSERD_TLS_CERT/KEY/ALLOW`).
- `src/client/run.sh` — wrapper do `unuser`: garante o keyfile (gera 0600 na 1ª vez, com
  aviso de que deve ser **copiado** — não regerado — entre máquinas) e repassa o subcomando.
- `src/client/run-gui.sh` — instala o extra `gui` (PySide6) e, em Debian 13/Qt 6.5+, baixa
  `libxcb-cursor0` **sem root** num cache local (`~/.cache/unuser/lib`) e lança a GUI.

## Fluxo de contribuição

Toda mudança entra na `main` por **branch → PR → squash-merge** (ver histórico: cada
commit da `main` é um PR `(#N)`). Não commite direto na `main`. Releases saem com `gh`
publicando os assets (`.deb` de `dist/`, PDFs de `doc/`, `SHA256SUMS.txt`); o `CHANGELOG.md`
acompanha cada versão. Bump de versão: `pyproject.toml` (`version`) + `CHANGELOG.md`.

## Roadmap

Fases 1–6 ✓ (cripto · chunker+índice · manifesto · servidor cofre-cego+mTLS · motor de
sync+CLI · GUI PySide6 (threaded) + SOCKS5/Tor · empacotamento `unuserd`/`unuser`
.deb/systemd). Refinos concluídos: salt cross-máquina, streaming no chunker, `.deb` do
cliente, **block_id por arquivo**, e a repaginação da GUI (tema escuro, Explorer de duas
áreas, adicionar arquivo com auto-registro de pasta, tela de pastas sincronizadas,
persistência do estado da árvore). Acesso remoto é **Tor** (sem VPN).
