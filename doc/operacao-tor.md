# unuser — Acesso remoto por Tor (Onion Service)

Fora da LAN, o `unuserd` é alcançado por um **Onion Service v3** do Tor. O endereço
`.onion` autentica o servidor (é derivado da chave pública dele) e o **mTLS** continua
autenticando o cliente. Não há IP público nem port-forward.

O `unuserd` **não fala Tor diretamente**: ele só escuta TLS em `127.0.0.1:8080`. Quem
publica o serviço oculto e encaminha o tráfego é o daemon `tor`.

## Servidor (máquina central)

1. Instale o Tor: `apt install tor`.
2. No `torrc` (`/etc/tor/torrc`):

   ```
   HiddenServiceDir /var/lib/tor/unuser/
   HiddenServicePort 8080 127.0.0.1:8080
   ```

3. `systemctl restart tor`. O endereço fica em `/var/lib/tor/unuser/hostname`
   (algo como `abcdef…xyz.onion`).
4. Rode o `unuserd` escutando em `127.0.0.1:8080` com o contexto mTLS
   (`common.tls.server_context`).

## Cliente

1. Instale o Tor (`apt install tor`); ele expõe um proxy SOCKS5 em `127.0.0.1:9050`.
2. Na interface, escolha o modo **Tor** e informe `<...>.onion` + porta `8080`
   (gravado no `~/.env` como `UNUSER_CONN_MODE=tor`, `UNUSER_TOR_ONION=…`,
   `UNUSER_TOR_SOCKS=127.0.0.1:9050`).
3. O cliente conecta ao `.onion:8080` **através** do SOCKS5 do Tor e faz o mTLS
   por dentro do túnel.

> Estado atual: o `VaultClient` fala mTLS direto (LAN) **e** tunelado pelo SOCKS5 do Tor
> (`VaultClient(..., socks_proxy=(host, port))`, montado por `config.ConnConfig.make_client`
> no modo `tor`). O CONNECT usa ATYP=domínio para o Tor resolver o `.onion`. Latência
> maior é esperada — aceitável porque a sincronização é manual e transfere só os blocos
> que mudaram.
