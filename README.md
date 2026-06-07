# unuser — UniqueUser

Sincronização **manual** de documentos entre máquinas Debian através de um servidor
central **cofre-cego** (*zero-knowledge*): o servidor (`unuserd`) só guarda blobs e
manifestos **cifrados** e nunca vê conteúdo, nomes de arquivos ou chaves. O cliente
(`unuser`) é um app gráfico **estilo Explorer em tema escuro** (PySide6) com CLI
equivalente; a sincronização é controlada por status e ações na interface (nada automático).

**Estado:** **v1.0.1** (cliente). Roadmap (Fases 1–6) concluído + refinos. ~166 testes
passando. Código aberto sob licença [MIT](LICENSE).

> **Manual do usuário** (instalação, configuração e uso, passo a passo):
> [`doc/manual-usuario-unuser.html`](doc/manual-usuario-unuser.html) (+ PDF).

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

## Requisitos

Base comum: **Python ≥ 3.11** (o código usa `X | None`). Alvo: **Debian 13 (trixie)** /
Ubuntu (os comandos abaixo usam `apt`; em outras distros, instale os equivalentes).

### Dependências de sistema (apt)

```bash
# 1) Base — clonar o código, Python, venv e pip
sudo apt install -y git python3 python3-venv python3-pip ca-certificates curl

# 2) GUI do cliente — bibliotecas de runtime do Qt/PySide6
sudo apt install -y libxcb-cursor0 libxkbcommon0 libgl1
#    (alternativa: usar o PySide6 do sistema em vez do pip)
#    sudo apt install -y python3-pyside6.qtwidgets

# 3) Servidor via .deb — o apt resolve sozinho ao instalar, mas se quiser adiantar:
sudo apt install -y python3-cryptography adduser

# 4) Opcionais
sudo apt install -y libreoffice     # regenerar os PDFs da documentação (soffice)
sudo apt install -y tor             # acesso remoto via Onion Service (Tor)
#    Construir os .deb não exige pacote extra: o dpkg-deb já vem com o Debian (pacote dpkg).
```

> **Sem `ensurepip`/`python3-venv`** (ou sem sudo)? Monte o venv sem pip e faça bootstrap:
> ```bash
> python3 -m venv --without-pip .venv
> curl -sS -o /tmp/get-pip.py https://bootstrap.pypa.io/get-pip.py
> .venv/bin/python /tmp/get-pip.py
> ```

### Dependências Python (instaladas pelo `pip` a partir do `pyproject.toml`)

| Grupo | Pacotes | Para quê | Como instalar |
|-------|---------|----------|---------------|
| runtime | `cryptography>=42`, `argon2-cffi>=23`, `blake3>=0.4` | cliente (cifra/decifra) e servidor com mTLS | `pip install -e .` |
| `gui` | `PySide6-Essentials>=6.7` | interface gráfica do cliente | `pip install -e ".[gui]"` |
| `dev` | `pytest>=8` | rodar os testes | `pip install -e ".[dev]"` |

Quem precisa de quê:

- **Cliente (CLI):** base apt (1) + `cryptography`/`argon2-cffi`/`blake3` (runtime).
- **Cliente (GUI):** o acima + apt (2) + extra `gui`.
- **Servidor:** base apt (1); `cryptography` só é necessária **se ligar o mTLS**
  (o cofre-cego puro roda só com a stdlib). **Não** precisa de PySide6/argon2/blake3.

```bash
# tudo do cliente, incluindo GUI e testes, num venv:
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[gui,dev]"
```

## Instalação

### Opção A — pacotes `.deb` (recomendado para usar)

Os `.deb` são montados a partir de `packaging/` (ver [`packaging/README.md`](packaging/README.md)).
Requer **Debian 13 (trixie)**.

**Servidor** (nó central, sempre ligado):

```bash
sh packaging/build-deb.sh                       # gera dist/unuserd_<versao>_all.deb
sudo apt install ./dist/unuserd_1.0.1_all.deb   # cria usuário 'unuser' + /var/lib/unuser/vault
sudoedit /etc/default/unuserd                   # storage/host/porta e, p/ mTLS, --tls-*
sudo systemctl start unuserd
```

**Cliente** (GUI/CLI):

```bash
sh packaging/build-deb-client.sh                # gera dist/unuser_<versao>_amd64.deb
sudo apt install ./dist/unuser_1.0.1_amd64.deb  # puxa python3-cryptography/-argon2; blake3 embarcado
```

> A **GUI** recomenda `python3-pyside6.qtwidgets` (`sudo apt install python3-pyside6.qtwidgets`).
> Em Debian 13/Qt 6.5+ o plugin xcb também precisa de **`libxcb-cursor0`**
> (`sudo apt install libxcb-cursor0`). O CLI de sync funciona sem nada disso.

### Opção C — só o cliente GUI, numa máquina nova (script tudo-em-um)

Baixa o código, monta o ambiente, instala a GUI, configura a conexão e abre a interface:

```bash
curl -fsSL https://raw.githubusercontent.com/eueocraudio/unuser/main/install-gui-client.sh | bash
```

Variáveis úteis (ver o cabeçalho de [`install-gui-client.sh`](install-gui-client.sh)):
`UNUSER_SERVER=host:porta` (servidor), `UNUSER_KEYFILE_SRC=usuario@maquina:~/.config/unuser/keyfile`
(copiar o keyfile do cofre **existente** — obrigatório, senão o cofre não abre),
`UNUSER_NEW_VAULT=1` (gerar keyfile para um cofre **novo**).

### Opção B — a partir do código (desenvolvimento)

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
.venv/bin/unuserd --storage /var/lib/unuser/vault --host 127.1.0.1 --port 8443 \
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

**Scripts de conveniência** (acham o binário do `.venv` ou do PATH; aplicam defaults):

```bash
src/server/run.sh                                # sobe o unuserd (UNUSERD_PORT=... p/ trocar)
UNUSER_PASSPHRASE=... src/client/run.sh status   # cliente (gera o keyfile na 1ª vez)
UNUSER_PASSPHRASE=... src/client/run-gui.sh      # instala PySide6 + libxcb-cursor0 e abre a GUI
```

**A interface gráfica** é um Explorer de duas áreas (tema escuro): à esquerda a **árvore de
pastas**, à direita a **lista de arquivos da pasta selecionada** (com 6 cores de status).
Botões: Atualizar/Enviar/Receber/Apagar, **Adicionar arquivo** (registra a pasta no
`dirs.json` se preciso) e **Pastas…** (gerenciar pastas sincronizadas). A expansão da árvore
e a pasta selecionada são lembradas entre sessões (em `~/.local/data/unuser/`).

## Empacotamento (.deb + systemd)

`packaging/` monta os dois pacotes **sem root** (ver [`packaging/README.md`](packaging/README.md)):

```bash
sh packaging/build-deb.sh          # dist/unuserd_*_all.deb   (servidor + unit systemd)
sh packaging/build-deb-client.sh   # dist/unuser_*_amd64.deb  (cliente; blake3 embarcado)
```

## Layout

```
src/client/   crypto, chunker, index, manifest, transport, scanner, sync, actions, config, cli, gui
              run.sh, run-gui.sh                  (scripts de execução do cliente)
src/server/   storage, http_server, cli           (cofre-cego; nunca decifra)
              run.sh                               (script de execução do servidor)
src/common/   tls                                  (contextos mTLS, compartilhado)
tests/        suíte pytest
packaging/    .deb (cliente e servidor) + systemd
doc/          especificação + manual do usuário (HTML/PDF), operação Tor, roteiro de testes
```

## Segurança em uma linha

A confidencialidade depende dos **dois fatores** (passphrase + keyfile), que nunca vão ao
servidor; o servidor é cego para conteúdo, nomes e chaves. O salt do Argon2 (público) e os
metadados de versão são o que ele enxerga. Acesso remoto é via Tor.
