#!/bin/sh
# Monta o .deb do CLIENTE (`unuser`) no modo binário manual, sem root.
#
# Estratégia: depende dos pacotes de sistema do Debian para o pesado
# (python3-cryptography, python3-argon2, e — Recommends — python3-pyside6.qtwidgets p/ a
# GUI) e **embarca só o blake3**, que não existe no apt. Como o blake3 é uma extensão
# compilada (cp313/amd64), o pacote é Architecture: amd64 e fixado ao python3.13.
#
# O blake3 vendorizado é copiado do venv de build (site-packages). Saída:
# dist/unuser_<versao>_amd64.deb
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VER=$(grep -m1 '^version' "$HERE/pyproject.toml" | sed 's/.*"\(.*\)".*/\1/')
PKG=unuser
OUT="${1:-$HERE/dist}"
VENV_SITE="$HERE/.venv/lib/python3.13/site-packages"
BLAKE3_SRC="$VENV_SITE/blake3"

[ -d "$BLAKE3_SRC" ] || { echo "erro: $BLAKE3_SRC ausente (rode o venv com blake3)"; exit 1; }

BUILD=$(mktemp -d)
ROOT="$BUILD/$PKG"
trap 'rm -rf "$BUILD"' EXIT

mkdir -p "$ROOT/DEBIAN" "$ROOT/usr/lib/unuser/client" "$ROOT/usr/lib/unuser/common" \
         "$ROOT/usr/bin" "$ROOT/usr/share/applications" "$ROOT/usr/share/doc/unuser"

# módulos do cliente (+ common, p/ mTLS futuro) — sem __pycache__
for f in "$HERE"/src/client/*.py; do cp "$f" "$ROOT/usr/lib/unuser/client/"; done
cp "$HERE"/src/common/*.py "$ROOT/usr/lib/unuser/common/"

# blake3 embarcado (vendored): copia o pacote (só o .so + __init__ + stubs)
mkdir -p "$ROOT/usr/lib/unuser/blake3"
cp "$BLAKE3_SRC"/blake3*.so "$BLAKE3_SRC/__init__.py" "$ROOT/usr/lib/unuser/blake3/"
[ -f "$BLAKE3_SRC/py.typed" ] && cp "$BLAKE3_SRC/py.typed" "$ROOT/usr/lib/unuser/blake3/" || true

# wrapper executável
cat > "$ROOT/usr/bin/unuser" <<'SH'
#!/bin/sh
exec env PYTHONPATH=/usr/lib/unuser /usr/bin/python3 -m client.cli "$@"
SH
chmod 0755 "$ROOT/usr/bin/unuser"

# atalho de menu da GUI (ícone de tema, sem assets proprietários)
cat > "$ROOT/usr/share/applications/unuser.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=unuser
Comment=Cofre cifrado (zero-knowledge) — sincronização manual
Exec=unuser gui
Terminal=false
Categories=Utility;Network;FileTransfer;
Icon=folder-remote
EOF

cp "$HERE/packaging/README.md" "$ROOT/usr/share/doc/unuser/" 2>/dev/null || true

cat > "$ROOT/DEBIAN/control" <<EOF
Package: $PKG
Version: $VER
Architecture: amd64
Maintainer: unuser maintainers <noreply@unuser.invalid>
Section: net
Priority: optional
Depends: python3 (>= 3.13), python3 (<< 3.14), python3-cryptography, python3-argon2
Recommends: python3-pyside6.qtwidgets
Description: unuser - cliente do cofre cifrado (zero-knowledge)
 App de sincronizacao manual entre maquinas via um servidor cofre-cego (unuserd).
 Cifra tudo localmente (o servidor nunca ve conteudo, nomes ou chaves). Expoe o
 executavel "unuser" (CLI: status/send/receive/delete) e a GUI estilo XP ("unuser gui",
 que requer python3-pyside6.qtwidgets). O blake3 vai embarcado por nao existir no apt;
 por isso o pacote e amd64 e fixado ao python3.13.
EOF

mkdir -p "$OUT"
DEB="$OUT/${PKG}_${VER}_amd64.deb"
dpkg-deb --root-owner-group --build "$ROOT" "$DEB" >/dev/null
echo "gerado: $DEB"
