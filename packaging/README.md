# Empacotamento (Fase 6)

## Servidor — `unuserd` (.deb + systemd)

O nó central (sempre ligado) roda o **cofre-cego**. Pacote enxuto: só os módulos
`server` + `common`, dependendo de `python3` e `python3-cryptography` (sem o cliente,
sem PySide6/blake3/argon2).

```bash
sh packaging/build-deb.sh          # gera dist/unuserd_<versao>_all.deb (sem root)
sudo apt install ./dist/unuserd_0.0.1_all.deb
```

A instalação cria o usuário de sistema `unuser`, o diretório `/var/lib/unuser/vault` e
**habilita** (não inicia) o serviço. Configure e suba:

```bash
sudoedit /etc/default/unuserd      # storage/host/porta e, p/ mTLS, --tls-cert/key/allow
sudo systemctl start unuserd
```

O `unuserd` escuta **só em localhost** — quem publica o serviço é o Onion Service do Tor
(ver [`../doc/operacao-tor.md`](../doc/operacao-tor.md)). A unit systemd
([`systemd/unuserd.service`](systemd/unuserd.service)) roda como o usuário `unuser` com
endurecimento (`ProtectSystem=strict`, `NoNewPrivileges`, `MemoryDenyWriteExecute`, etc.).

Inspecionar o pacote sem instalar: `dpkg-deb -I dist/unuserd_*.deb` / `dpkg-deb -c …`.

## Cliente — `unuser` (GUI/CLI)

O cliente é manual (GUI/CLI, sem daemon) e tem dependências pesadas (PySide6, blake3,
argon2-cffi) que nem sempre estão empacotadas no Debian. Por isso a instalação
recomendada do cliente é **pip em um venv** (ver `CLAUDE.md` → "Ambiente"):

```bash
.venv/bin/python -m pip install -e ".[gui]"   # expõe os executáveis `unuser` e `unuserd`
unuser gui
```

Um `.deb` do cliente (via `dh-virtualenv` ou venv embarcado) fica como trabalho futuro.
