# unuser — Roteiro de teste manual

Validação **a olho/à mão** das garantias do unuser, complementando a suíte automatizada
(`.venv/bin/python -m pytest`). Cada passo tem **ação** e **resultado esperado**; marque
o `[ ]` ao confirmar.

O estado atual cobre as **Fases 1–4** (cripto, chunker, índice, manifesto, servidor
cofre-cego + mTLS). Ainda **não há** entry point `unuserd` nem a GUI — então o servidor
é exercitado por um pequeno snippet Python. A **Parte F** (GUI) fica como checklist para
quando a Fase 5 existir.

> Pré-requisito: venv montado conforme o `CLAUDE.md`. Todos os comandos assumem o
> diretório raiz do projeto e o interpretador `.venv/bin/python`.

---

## Parte A — Servidor cofre-cego no ar (HTTP simples)

**A1. Subir o servidor.** Num terminal:

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from server.storage import BlindStorage
from server.http_server import make_server
httpd = make_server(BlindStorage("/tmp/unuser-vault"), port=8080)
print("cofre-cego ouvindo em", httpd.server_address)
httpd.serve_forever()
PY
```

- [ ] Imprime `cofre-cego ouvindo em ('127.0.0.1', 8080)` e fica rodando.

**A2. Health check.** Noutro terminal:

```bash
curl -s http://127.0.0.1:8080/healthz
```

- [ ] Responde `ok`.

**A3. Enviar e baixar um blob** (id válido = `b:` + 64 hex):

```bash
BID="b:abababababababababababababababababababababababababababababababab"
printf 'bytes-ja-cifrados-pelo-cliente' | curl -s -X PUT --data-binary @- "http://127.0.0.1:8080/blob/$BID"
echo; curl -s "http://127.0.0.1:8080/blob/$BID"; echo
```

- [ ] O PUT responde `ok`; o GET devolve exatamente `bytes-ja-cifrados-pelo-cliente`.
- [ ] Repetir o PUT com outro conteúdo **não** sobrescreve (endereçado por conteúdo):
      o GET continua devolvendo o primeiro.

**A4. block_id inválido é recusado** (o servidor só aceita `b:` + 64 hex):

```bash
# id no formato errado (não-hexadecimal) -> chega ao servidor e é barrado pelo regex
curl -s -o /dev/null -w 'nao-hex=%{http_code}\n' -X PUT --data-binary 'x' \
  "http://127.0.0.1:8080/blob/b:zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"
# id com '../' -> o próprio curl normaliza para /etc/pwn antes de enviar -> 404
curl -s -o /dev/null -w 'traversal=%{http_code}\n' -X PUT --data-binary 'x' \
  "http://127.0.0.1:8080/blob/../../etc/pwn"
```

- [ ] `nao-hex=400` (regex anti-traversal recusa o id malformado).
- [ ] `traversal=404` — o `curl` colapsa o `../` no cliente, então o servidor nem vê um
      caminho de blob. Em nenhum dos casos algo é gravado: `ls /tmp/unuser-vault/blobs/`
      mostra só o blob legítimo do A3.

> Nota: a defesa do regex contra um `../` **cru** (sem a normalização do curl) é provada
> em `tests/test_security.py::test_path_traversal_no_http_e_rejeitado`, que envia o id
> bruto pela `VaultClient`.

**A5. Manifesto com CAS (compare-and-swap).**

```bash
H='-H X-Unuser-Expected-Version:0 -H X-Unuser-New-Version:1'
curl -s -o /dev/null -w 'v1=%{http_code}\n' $H -X PUT --data-binary 'manifesto-cifrado-1' http://127.0.0.1:8080/manifest
curl -s -i http://127.0.0.1:8080/manifest | grep -i x-unuser-version
# agora um cliente "atrasado" tenta gravar baseado na versão 0 de novo:
curl -s -o /dev/null -w 'conflito=%{http_code}\n' \
  -H 'X-Unuser-Expected-Version:0' -H 'X-Unuser-New-Version:2' \
  -X PUT --data-binary 'perdido' http://127.0.0.1:8080/manifest
```

- [ ] Primeiro PUT: `v1=200`; o GET mostra `X-Unuser-Version: 1`.
- [ ] O segundo PUT (baseado na versão obsoleta) responde `conflito=409` — o servidor
      protegeu contra *lost update*.

---

## Parte B — mTLS: só clientes na allowlist entram

Gera os certs e sobe o servidor já com TLS mútuo:

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
import threading, tempfile
from common import tls
from client.transport import VaultClient
from server.http_server import make_server
from server.storage import BlindStorage

d = tempfile.mkdtemp()
s_crt, s_key   = tls.generate_self_signed("unuserd",  d, "server")
ok_crt, ok_key = tls.generate_self_signed("amigo",    d, "ok")
bad_crt, bad_key = tls.generate_self_signed("intruso", d, "bad")
allow = tls.write_allowlist([ok_crt], f"{d}/allow.pem")     # só o "amigo"

sctx = tls.server_context(s_crt, s_key, allow)
httpd = make_server(BlindStorage(f"{d}/vault"), ssl_context=sctx)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()

ok  = VaultClient("127.0.0.1", port, ssl_context=tls.client_context(ok_crt,  ok_key,  s_crt))
bad = VaultClient("127.0.0.1", port, ssl_context=tls.client_context(bad_crt, bad_key, s_crt))

print("cliente autorizado  -> health:", ok.health())
try:
    bad.health()
    print("cliente intruso     -> ENTROU (FALHA!)")
except Exception as e:
    print("cliente intruso     -> recusado no handshake:", type(e).__name__)
PY
```

- [ ] `cliente autorizado -> health: True`.
- [ ] `cliente intruso -> recusado no handshake: ...` (cert fora da allowlist é barrado).

---

## Parte C — Conferir "a olho" que o servidor é cego

Depois de rodar a suíte de segurança (que faz um fluxo E2E real para um cofre em disco):

```bash
.venv/bin/python -m pytest tests/test_security.py -q
```

- [ ] `10 passed`. Entre elas, `test_servidor_so_armazena_ciphertext` prova que o
      plaintext e o nome do arquivo nunca aparecem no disco do servidor.

Inspeção manual dos blobs gravados na Parte A:

```bash
ls -la /tmp/unuser-vault/blobs/
cat /tmp/unuser-vault/manifest/current.json     # só {version, blob} — nada de conteúdo
```

- [ ] Os blobs são bytes opacos; o `current.json` guarda só metadados de versão.
      Nada no diretório revela conteúdo, nomes de arquivos ou chaves.

---

## Parte D — Verificar os dois fatores (passphrase + keyfile)

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from client import crypto, manifest
salt = crypto.generate_salt()
kr = crypto.unlock("minha-senha", b"meu-keyfile", salt)
sealed = manifest.seal(manifest.VaultManifest.new("cofre", salt), kr.manifest_key)

# keyfile errado (mesma senha) -> não abre
try:
    manifest.open_sealed(sealed, crypto.unlock("minha-senha", b"keyfile-errado", salt).manifest_key)
    print("keyfile errado abriu (FALHA!)")
except Exception as e:
    print("keyfile errado     -> bloqueado:", type(e).__name__)

# os dois certos -> abre
print("dois fatores certos -> abre:", manifest.open_sealed(sealed, kr.manifest_key).vault_id)
PY
```

- [ ] `keyfile errado -> bloqueado: InvalidTag`.
- [ ] `dois fatores certos -> abre: cofre`. (Confirma: nem a senha sozinha basta.)

---

## Parte E — Acesso remoto por Tor (opcional)

Seguir `doc/operacao-tor.md`: publicar o Onion Service apontando para o
`127.0.0.1:8080` do servidor e, do cliente, conectar ao `<...>.onion:8080`.

- [ ] Servidor TLS no ar em `127.0.0.1:8080` (Parte B com `port=8080`).
- [ ] `/var/lib/tor/unuser/hostname` traz o endereço `.onion`.
- [ ] (Fase 5) Cliente conecta pelo SOCKS5 do Tor e completa o mTLS por dentro do túnel.

> O tunelamento SOCKS5 no `VaultClient` ainda **não** está implementado (Fase 5);
> por ora valida-se só o lado servidor do Onion Service.

---

## Parte F — GUI estilo XP (Fase 5 — pendente)

Checklist a exercitar quando a interface PySide6 existir. Marcar como `N/A` até lá.

- [ ] Primeiro uso: configurar **passphrase + keyfile**; confirmar que nenhum dos dois
      é gravado em disco (só geram a KEK em memória).
- [ ] Treeview estilo Explorer mostra os arquivos com **status** correto
      (sincronizado / modificado local / novo no servidor / conflito).
- [ ] As **4 ações** disparam o esperado (enviar, receber, resolver conflito, apagar).
- [ ] **Enviar**: editar um arquivo grande e confirmar que só os **blocos alterados**
      sobem (transferência por blocos / dedup).
- [ ] **Receber**: alterar pela outra máquina e puxar; o arquivo local reflete a mudança.
- [ ] **Conflito**: editar nos dois lados sem sincronizar → a UI sinaliza o conflito
      (o servidor recusou via CAS/409) e oferece resolução.
- [ ] **Apagar**: arquivo some da árvore mas o histórico (tombstone) é preservado.
- [ ] Alternar **modo de conexão IP (LAN) ↔ Tor** e confirmar que ambos sincronizam.
