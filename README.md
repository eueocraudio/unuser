# unuser — UniqueUser

Sincronização **manual** de documentos entre máquinas Debian através de um servidor
central **cofre-cego** (*zero-knowledge*): o servidor (`unuserd`) só guarda blobs e
manifestos **cifrados** e nunca vê conteúdo, nomes de arquivos ou chaves. O cliente
(`unuser`) é um app gráfico estilo *Windows XP Explorer* (PySide6) com CLI equivalente; a
sincronização é controlada por status e ações na interface (nada automático).

**Estado:** roadmap (Fases 1–6) concluído + refinos. 154 testes passando. Repositório
privado.

## Como funciona (resumo)

- **Criptografia em envelope** (`src/client/crypto.py`): `root_key` derivada de
  **passphrase + keyfile** (Argon2id + HKDF — os dois fatores são obrigatórios e nunca
  são gravados); dela saem KEK, name_key, manifest_key, block_id_key. Cada arquivo tem
  uma FK aleatória, guardada no manifesto **embrulhada** pela KEK (rotação da chave-mestra
  não recriptografa conteúdo). AES-256-GCM, HMAC-SHA256, BLAKE3.
- **Chunking por conteúdo** (Gear hash determinístico) com dedup por bloco; em
  **streaming** (memória O(janela), não O(arquivo)).
- **Manifesto** cifrado com histórico por arquivo, tombstones e CAS (compare-and-swap)
  para concorrência. O **salt do Argon2** viaja em claro no cabeçalho do manifesto
  (parâmetro público de KDF) para uma máquina nova abrir o cofre sem cópia manual.
- **Servidor cofre-cego**: API HTTP stdlib, validação anti path-traversal, **mTLS**
  (allowlist de certs). Acesso remoto por **Tor** (Onion Service + SOCKS5); sem VPN.

A especificação canônica está em [`doc/especificacao-unuser.html`](doc/especificacao-unuser.html)
(+ PDF). Detalhes de arquitetura e convenções para contribuir: [`CLAUDE.md`](CLAUDE.md).

## Setup (do zero)

Precisa de **Python ≥ 3.11**. Crie um venv e instale em modo editável:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"      # runtime + pytest
.venv/bin/python -m pip install -e ".[gui]"      # opcional: PySide6 para a GUI
```

> Em máquinas **sem `ensurepip`/`python3-venv`** (e sem sudo), monte o venv sem pip e
> faça bootstrap com `get-pip.py` (ver a seção "Ambiente" do `CLAUDE.md`):
> ```bash
> python3 -m venv --without-pip .venv
> curl -sS -o /tmp/get-pip.py https://bootstrap.pypa.io/get-pip.py
> .venv/bin/python /tmp/get-pip.py
> .venv/bin/python -m pip install -e ".[dev]"
> ```

Rodar os testes:

```bash
.venv/bin/python -m pytest                       # todos
.venv/bin/python -m pytest tests/test_crypto.py -v
```

## Uso

**Servidor** (nó central, sempre ligado — escuta só em localhost; o Tor publica o serviço):

```bash
.venv/bin/unuserd --storage /var/lib/unuser/vault --host 127.0.0.1 --port 8443 \
  [--tls-cert server.crt --tls-key server.key --tls-allow allow.pem]   # mTLS opcional
```

**Cliente** — config em `~/.env` (conexão), `~/.config/unuser/dirs.json` (o que entra no
cofre) e `~/.unuserignore` (exclusões); passphrase via prompt ou `UNUSER_PASSPHRASE`,
keyfile via `--keyfile`/`UNUSER_KEYFILE` (ver §9 da spec):

```bash
.venv/bin/unuser status                          # compara local × servidor
.venv/bin/unuser send [vault/path ...]           # envia (sem args: tudo que mudou)
.venv/bin/unuser receive <vault/path ...>        # baixa do servidor
.venv/bin/unuser delete <vault/path ...>         # apaga (tombstone + remove local)
.venv/bin/unuser gui                              # interface gráfica (precisa do extra gui)
```

## Empacotamento (.deb + systemd)

`packaging/` monta os dois pacotes **sem root** (ver [`packaging/README.md`](packaging/README.md)):

```bash
sh packaging/build-deb.sh          # dist/unuserd_*_all.deb   (servidor + unit systemd)
sh packaging/build-deb-client.sh   # dist/unuser_*_amd64.deb  (cliente; blake3 embarcado)
```

## Layout

```
src/client/   crypto, chunker, index, manifest, transport, scanner, sync, actions, config, cli, gui
src/server/   storage, http_server, cli           (cofre-cego; nunca decifra)
src/common/   tls                                  (contextos mTLS, compartilhado)
tests/        suíte pytest
packaging/    .deb (cliente e servidor) + systemd
doc/          especificação (HTML/PDF), operação Tor, roteiro de testes manual
```

## Segurança em uma linha

A confidencialidade depende dos **dois fatores** (passphrase + keyfile), que nunca vão ao
servidor; o servidor é cego para conteúdo, nomes e chaves. O salt do Argon2 (público) e os
metadados de versão são o que ele enxerga. Acesso remoto é via Tor.
