# Changelog

Todas as mudanças notáveis do **unuser**. Formato baseado em
[Keep a Changelog](https://keepachangelog.com/pt-BR/1.0.0/); versionamento
[SemVer](https://semver.org/lang/pt-BR/).

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
