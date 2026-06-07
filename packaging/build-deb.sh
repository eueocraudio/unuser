#!/bin/sh
# Monta o pacote .deb do servidor `unuserd` no modo binário manual — sem root, sem
# fakeroot, sem dpkg-buildpackage. Usa `dpkg-deb --root-owner-group` para forçar a posse
# root:root dentro do pacote. Empacota só `server` + `common` (o cofre-cego não precisa
# do cliente). Saída: dist/unuserd_<versao>_all.deb
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VER=$(grep -m1 '^version' "$HERE/pyproject.toml" | sed 's/.*"\(.*\)".*/\1/')
PKG=unuserd
OUT="${1:-$HERE/dist}"
BUILD=$(mktemp -d)
ROOT="$BUILD/$PKG"
trap 'rm -rf "$BUILD"' EXIT

mkdir -p "$ROOT/DEBIAN" \
         "$ROOT/usr/lib/unuserd/server" "$ROOT/usr/lib/unuserd/common" \
         "$ROOT/usr/bin" "$ROOT/lib/systemd/system" "$ROOT/etc/default" \
         "$ROOT/usr/share/doc/unuserd"

# módulos python necessários ao daemon
cp "$HERE"/src/server/__init__.py "$HERE"/src/server/storage.py \
   "$HERE"/src/server/http_server.py "$HERE"/src/server/cli.py \
   "$ROOT/usr/lib/unuserd/server/"
cp "$HERE"/src/common/__init__.py "$HERE"/src/common/tls.py "$ROOT/usr/lib/unuserd/common/"

# wrapper executável
cat > "$ROOT/usr/bin/unuserd" <<'SH'
#!/bin/sh
exec env PYTHONPATH=/usr/lib/unuserd /usr/bin/python3 -m server.cli "$@"
SH
chmod 0755 "$ROOT/usr/bin/unuserd"

cp "$HERE/packaging/systemd/unuserd.service" "$ROOT/lib/systemd/system/unuserd.service"
cp "$HERE/packaging/default/unuserd" "$ROOT/etc/default/unuserd"
cp "$HERE/doc/operacao-tor.md" "$ROOT/usr/share/doc/unuserd/" 2>/dev/null || true

cat > "$ROOT/DEBIAN/control" <<EOF
Package: $PKG
Version: $VER
Architecture: all
Maintainer: unuser maintainers <noreply@unuser.invalid>
Section: net
Priority: optional
Depends: python3 (>= 3.11), python3-cryptography, adduser
Description: unuser - servidor cofre-cego (zero-knowledge)
 Armazena blobs e manifestos cifrados pelo cliente e nunca ve conteudo,
 nomes ou chaves. Autentica clientes por mTLS e e alcancado via Tor.
EOF

echo "/etc/default/unuserd" > "$ROOT/DEBIAN/conffiles"

cat > "$ROOT/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
  if ! getent passwd unuser >/dev/null 2>&1; then
    adduser --system --group --home /var/lib/unuser --no-create-home --quiet unuser
  fi
  mkdir -p /var/lib/unuser
  chown unuser:unuser /var/lib/unuser
  chmod 0750 /var/lib/unuser

  # --- diretório de persistência (configurável em /etc/default/unuserd) -----
  # Lê UNUSERD_STORAGE do conffile; cria e dá posse ao caminho configurado (que pode
  # estar fora de /var/lib/unuser — nesse caso a unit precisa de um drop-in ReadWritePaths).
  # Extrai com grep (NÃO `.`/source: UNUSERD_ARGS tem espaços e quebraria o source).
  STORAGE=$(grep -E '^[[:space:]]*UNUSERD_STORAGE=' /etc/default/unuserd 2>/dev/null \
            | tail -n1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//; s/^[[:space:]]*//; s/[[:space:]]*$//')
  STORAGE="${STORAGE:-/var/lib/unuser/vault}"
  mkdir -p "$STORAGE"
  chown -R unuser:unuser "$STORAGE"
  chmod 0750 "$STORAGE"
  case "$STORAGE" in
    /var/lib/unuser/*) ;;
    *) echo "unuserd: storage em $STORAGE (fora de /var/lib/unuser) — adicione" \
            "'ReadWritePaths=$STORAGE' via 'systemctl edit unuserd' senão a gravação falha." >&2 ;;
  esac

  # --- material mTLS (identidade TLS do servidor) ---------------------------
  # O cofre-cego escuta em localhost e é publicado pelo Onion Service do Tor; o mTLS
  # (opcional) autentica os CLIENTES por dentro do túnel. Geramos aqui, uma única vez
  # (idempotente), o par do servidor e uma allowlist VAZIA. O mTLS fica gerado-mas-
  # desligado: allowlist vazia + CERT_REQUIRED recusaria todo cliente. O operador
  # adiciona os certs de cliente em /etc/unuser/allow.pem e liga as flags --tls-* em
  # /etc/default/unuserd (instruções no próprio arquivo).
  mkdir -p /etc/unuser
  if [ ! -f /etc/unuser/server.key ]; then
    if PYTHONPATH=/usr/lib/unuserd python3 - <<'PY' ; then :; else
from common import tls
crt, _ = tls.generate_self_signed("unuserd", "/etc/unuser", "server")
print("unuserd: par mTLS do servidor gerado em /etc/unuser/server.{crt,key}")
print("unuserd: fingerprint do servidor (informe aos clientes): "
      + tls.cert_fingerprint(crt))
PY
      echo "unuserd: aviso — não consegui gerar o par mTLS (cryptography ausente?);" \
           "o servidor roda sem mTLS (só localhost + Tor). Gere depois se for usar mTLS." >&2
    fi
  fi
  [ -f /etc/unuser/allow.pem ] || : > /etc/unuser/allow.pem
  chown -R unuser:unuser /etc/unuser
  chmod 0750 /etc/unuser
  chmod 0640 /etc/unuser/allow.pem 2>/dev/null || true

  if [ -d /run/systemd/system ]; then
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl enable unuserd.service >/dev/null 2>&1 || true
    # não inicia sozinho: ligue o Tor/mTLS e rode `systemctl start unuserd`
  fi
fi
exit 0
EOF

cat > "$ROOT/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = remove ] || [ "$1" = deconfigure ]; then
  [ -d /run/systemd/system ] && systemctl stop unuserd.service >/dev/null 2>&1 || true
fi
exit 0
EOF

cat > "$ROOT/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = remove ] || [ "$1" = purge ]; then
  if [ -d /run/systemd/system ]; then
    systemctl disable unuserd.service >/dev/null 2>&1 || true
    systemctl daemon-reload >/dev/null 2>&1 || true
  fi
fi
if [ "$1" = purge ]; then
  rm -rf /etc/unuser            # identidade mTLS gerada no postinst (recriada num reinstall)
fi
exit 0
EOF

chmod 0755 "$ROOT/DEBIAN/postinst" "$ROOT/DEBIAN/prerm" "$ROOT/DEBIAN/postrm"

mkdir -p "$OUT"
DEB="$OUT/${PKG}_${VER}_all.deb"
dpkg-deb --root-owner-group --build "$ROOT" "$DEB" >/dev/null
echo "gerado: $DEB"
