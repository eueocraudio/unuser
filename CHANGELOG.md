# Changelog

Todas as mudanças notáveis do **unuser**. Formato baseado em
[Keep a Changelog](https://keepachangelog.com/pt-BR/1.0.0/); versionamento
[SemVer](https://semver.org/lang/pt-BR/).

## [1.3.0] — 2026-06-07

### Adicionado
- **Servidor — uso de disco** (`GET /usage`): reporta o uso do disco **físico** onde fica o
  storage (`shutil.disk_usage`) + o tamanho do cofre. É metadado de infra (não vaza
  conteúdo/nomes/chaves). O cliente mostra uma **barra de uso** no rodapé da GUI.
- **Servidor — backup (export/import) server-side**, disparado por **botões no cliente**: o
  servidor empacota `blobs/`+`manifest/` num `.tar` (`POST /backups`), lista os backups
  (`GET /backups`) e restaura (`POST /restore`, **destrutivo**, com confirmação na GUI). O
  diretório dos backups é configurável via **`UNUSERD_BACKUPS`** (ex.: mídia secundária);
  default `<storage>/backups`. Restauração troca os diretórios via rename (sob lock) e valida
  o `.tar` contra path-traversal.
- **GUI — tela "Servidor…"** (`ServerDialog`): uso de disco + exportar/restaurar backups.

[1.3.0]: https://github.com/eueocraudio/unuser/releases/tag/v1.3.0

## [1.2.0] — 2026-06-07

### Adicionado
- **GUI — colunas de Tamanho e Data** na lista de arquivos (passa de 2 para 4 colunas:
  Arquivo, Status, Tamanho, Data) e **ordenação** ao clicar no cabeçalho, por **nome,
  status, tamanho e data**. A ordenação é por **valor** (tamanho numérico, data
  cronológica), não pelo texto exibido. Tamanho/data vêm do disco local ou, para arquivos
  que só existem no servidor, da versão atual no manifesto (`FileState.size`/`.mtime`).

[1.2.0]: https://github.com/eueocraudio/unuser/releases/tag/v1.2.0

## [1.1.0] — 2026-06-07

Servidor mais fácil de operar, projeto aberto (MIT) e correção do timeout em arquivos
grandes.

### Adicionado
- **Servidor — par mTLS gerado na instalação.** O `postinst` gera
  `/etc/unuser/server.{crt,key}` (idempotente) e imprime o *fingerprint*; cria uma
  allowlist vazia. O mTLS nasce **gerado-mas-desligado** (allowlist vazia +
  `CERT_REQUIRED` recusaria todos os clientes); liga-se adicionando certs de cliente.
- **Servidor — diretório de persistência configurável** via `UNUSERD_STORAGE` em
  `/etc/default/unuserd` (o `postinst` cria/dá posse; fora de `/var/lib/unuser` exige
  drop-in `ReadWritePaths`).
- **Licença MIT** (`LICENSE`) — o projeto agora é open source.
- **Instalador do cliente GUI** (`install-gui-client.sh`): tudo-em-um (clona o repo
  público, monta o venv com fallback `get-pip.py`, instala a GUI, configura `~/.env`/
  `dirs.json` e abre a interface), com tratamento cuidadoso do keyfile.
- **README — seção "Requisitos"** com pacotes de sistema, dependências Python por grupo e
  os comandos de instalação.

### Alterado
- **Porta padrão 8443 → 8080** (cliente, servidor, scripts, documentação e testes).
- **Blocos maiores: `4/16/64 KiB` → `32/128/512 KiB`.** Quem define o tamanho típico é o
  `AVG_SIZE` (máscara do Gear hash). Resultado: ~8× menos blocos/requests por arquivo e
  manifesto menor. *Muda as fronteiras de chunking → a dedup só casa entre clientes nesta
  configuração (todos precisam desta versão).*

### Corrigido
- **Crítico — timeout ao enviar arquivos grandes.** O cliente abria **uma conexão TCP por
  bloco**; um arquivo grande são milhares de blocos, esgotando portas efêmeras
  (`TIME_WAIT`) até o `connect()` travar e estourar o timeout. Agora o `VaultClient` reusa
  **uma conexão persistente** (keep-alive, com `TCP_NODELAY` para evitar o stall de
  Nagle+delayed-ACK), reconectando uma vez se a conexão cair. Reprodução real contra o
  servidor: **timeout → 4,8 s**; 80 MB passam de 4218 conexões para **1**.
- **`receive` numa máquina nova** não estoura mais `KeyError: raiz desconhecida para o
  prefixo …`. Quando o prefixo não corresponde a nenhuma raiz configurada, o arquivo é
  baixado em `~/<prefixo>/` (criando a pasta).

[1.1.0]: https://github.com/eueocraudio/unuser/releases/tag/v1.1.0

## [1.0.1] — 2026-06-04

Primeira versão marcada do **cliente** (`unuser`). Inclui um corte de bug crítico de
integridade e a repaginação completa da interface gráfica.

### Corrigido
- **Crítico — `block_id` por arquivo (corrupção de dedup entre arquivos).** Blocos de
  conteúdo idêntico em arquivos diferentes geravam o mesmo `block_id` (chave global) e
  colapsavam num único blob no servidor (endereçado por conteúdo), cifrado com a FK de só
  um dos arquivos — deixando o(s) outro(s) **irrecuperável(is)** ao decifrar. Agora a chave
  do `block_id` é derivada **por arquivo** da FK (`crypto.derive_block_id_key`, HKDF). A
  dedup **dentro** do mesmo arquivo é preservada; não há migração (só muda a escrita).
  *Cofres já corrompidos: reenviar os arquivos afetados regrava blobs corretos.*

### Adicionado
- **GUI — layout Explorer de duas áreas, tema escuro.** À esquerda a árvore de **pastas**;
  à direita a **lista** dos arquivos **diretamente** na pasta selecionada (não recursivo),
  com as 6 cores de status.
- **GUI — ícones** de pasta e arquivo (ícones padrão do Qt, sem assets proprietários) na
  árvore, na lista e na tela de pastas.
- **GUI — botão "Adicionar arquivo".** Escolhe um arquivo e o envia ao cofre; se ele estiver
  fora das pastas sincronizadas mas dentro de `~/`, registra a **pasta** automaticamente (ou
  o arquivo como **item avulso**, se estiver direto em `~/` — a home inteira nunca é
  sincronizada).
- **GUI — tela "Pastas…"** para listar, adicionar e remover pastas sincronizadas e itens
  avulsos.
- **GUI — persistência do estado da árvore** (expansão por nó + pasta selecionada) entre
  reconstruções **e entre sessões**, em `~/.local/data/unuser/gui-state.json`.
- **Scripts de execução:** `src/server/run.sh`, `src/client/run.sh` e
  `src/client/run-gui.sh` (este instala PySide6 e resolve `libxcb-cursor0` sem root).
- **Configuração mutável:** `ContentConfig.save`/`add_dir`/`add_item`/`remove_dir`/
  `remove_item` e `resolve_or_register` (mapeia caminho local → vault path, registrando a
  raiz quando preciso).
- **Documentação:** manual do usuário (`doc/manual-usuario-unuser.html` + PDF) com
  instalação por `.deb` e do código; `README.md`/`CLAUDE.md` atualizados.

### Alterado
- **GUI** não fica mais presa em rede lenta: operações rodam fora da thread da UI
  (`QThreadPool`), com estado "ocupado".
- **Empacotamento:** `.deb` do cliente (`unuser`, amd64, `blake3` embarcado) e do servidor
  (`unuserd`, all) + unit systemd endurecida; montados sem root.

### Segurança
- Transporte remoto por **Tor** (Onion Service + SOCKS5) com **mTLS** por dentro do túnel;
  o servidor escuta só em `localhost`. O cofre permanece **zero-knowledge** (o servidor
  nunca vê conteúdo, nomes ou chaves). Confidencialidade depende de **passphrase + keyfile**
  (nunca gravados/transmitidos).

[1.0.1]: https://github.com/eueocraudio/unuser/releases/tag/v1.0.1
