# Empacotamento (Fase 6)

## Servidor — `unuserd` (.deb + systemd)

O nó central (sempre ligado) roda o **cofre-cego**. Pacote enxuto: só os módulos
`server` + `common`, dependendo de `python3` e `python3-cryptography` (sem o cliente,
sem PySide6/blake3/argon2).

```bash
sh packaging/build-deb.sh          # gera dist/unuserd_<versao>_all.deb (sem root)
sudo apt install ./dist/unuserd_1.0.1_all.deb
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

## Cliente — `unuser` (GUI/CLI, .deb)

O cliente (GUI/CLI, sem daemon) depende dos pacotes de **sistema** do Debian para o
pesado e **embarca só o `blake3`** (que não existe no apt):

- `Depends: python3 (>=3.13, <<3.14), python3-cryptography, python3-argon2`
- `Recommends: python3-pyside6.qtwidgets` (só a GUI; o CLI de sync funciona sem)
- `blake3` vendorizado em `/usr/lib/unuser/blake3` → por ser extensão compilada
  (cp313/amd64), o pacote é **amd64** e fixado ao python3.13.

```bash
sh packaging/build-deb-client.sh    # gera dist/unuser_<versao>_amd64.deb (sem root)
sudo apt install ./dist/unuser_1.0.1_amd64.deb
unuser gui          # ou: unuser status / send / receive / delete
```

> O build copia o `blake3` do venv de build (`.venv/lib/python3.13/site-packages/blake3`),
> então rode `pip install -e ".[dev]"` antes. O `.deb` foi verificado rodando com o
> **python3 do sistema** (fora do venv): blake3 vendorizado em uso e um `send`/`status`
> ponta-a-ponta contra um `unuserd`.

Config do cliente (§9): `~/.env`, `~/.config/unuser/dirs.json`, `~/.unuserignore`,
passphrase via `getpass`/`UNUSER_PASSPHRASE`, keyfile via `--keyfile`/`UNUSER_KEYFILE`.

Alternativa para desenvolvimento: `pip install -e ".[gui]"` num venv (expõe `unuser` e
`unuserd`).
