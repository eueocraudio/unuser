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
  mkdir -p /var/lib/unuser/vault
  chown -R unuser:unuser /var/lib/unuser
  chmod 0750 /var/lib/unuser
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
exit 0
EOF

chmod 0755 "$ROOT/DEBIAN/postinst" "$ROOT/DEBIAN/prerm" "$ROOT/DEBIAN/postrm"

mkdir -p "$OUT"
DEB="$OUT/${PKG}_${VER}_all.deb"
dpkg-deb --root-owner-group --build "$ROOT" "$DEB" >/dev/null
echo "gerado: $DEB"
