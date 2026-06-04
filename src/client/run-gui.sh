#!/usr/bin/env bash
# Instala (idempotente) e executa a interface gráfica (PySide6) do unuser.
#
# Resolve as dependências SEM root:
#   1. garante o extra `gui` (PySide6) no .venv do projeto;
#   2. em Debian + Qt 6.5+, o plugin xcb precisa de libxcb-cursor.so.0, que não vem
#      instalada por padrão; se faltar, baixa o pacote num cache local (sem sudo) e a
#      expõe via LD_LIBRARY_PATH;
#   3. lança `unuser gui` via run.sh (que cuida do keyfile e acha o binário).
#
# Variáveis de ambiente úteis (a GUI não tem prompt — defina a passphrase aqui):
#   UNUSER_PASSPHRASE  passphrase do cofre
#   UNUSER_KEYFILE     keyfile (default ~/.config/unuser/keyfile, gerado por run.sh)
#
# Exemplo:
#   UNUSER_PASSPHRASE='...' src/client/run-gui.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
venv="$root/.venv"
py="$venv/bin/python"

if [ ! -x "$py" ]; then
  echo "run-gui: .venv não encontrado em $venv — monte-o primeiro (veja CLAUDE.md/README)." >&2
  exit 1
fi

# 1) Garante o PySide6 (extra gui), idempotente.
if ! "$py" -c "import PySide6" 2>/dev/null; then
  echo "run-gui: instalando PySide6 (extra gui)…" >&2
  "$py" -m pip install -e "$root[gui]"
fi

# 2) Debian/Qt 6.5+: plugin xcb precisa de libxcb-cursor.so.0 (ausente por padrão).
plugin="$("$py" -c 'import PySide6, pathlib; print(pathlib.Path(PySide6.__file__).parent / "Qt/plugins/platforms/libqxcb.so")' 2>/dev/null || true)"
cache="${XDG_CACHE_HOME:-$HOME/.cache}/unuser/lib"
if [ -n "$plugin" ] && [ -f "$plugin" ] && ldd "$plugin" 2>/dev/null | grep -q "libxcb-cursor.so.0 .*not found"; then
  if [ ! -e "$cache/libxcb-cursor.so.0" ]; then
    if command -v apt-get >/dev/null && command -v dpkg-deb >/dev/null; then
      echo "run-gui: obtendo libxcb-cursor0 (sem root) em $cache…" >&2
      tmp="$(mktemp -d)"
      ( cd "$tmp" && apt-get download libxcb-cursor0 && dpkg-deb -x libxcb-cursor0_*.deb root ) >/dev/null 2>&1 || \
        echo "run-gui: falha ao baixar/extrair libxcb-cursor0." >&2
      mkdir -p "$cache"
      find "$tmp/root" -name "libxcb-cursor.so*" -exec cp -a {} "$cache/" \; 2>/dev/null || true
      rm -rf "$tmp"
    else
      echo "run-gui: falta libxcb-cursor.so.0 e não há apt-get/dpkg-deb." >&2
      echo "         instale o pacote de sistema: sudo apt-get install -y libxcb-cursor0" >&2
    fi
  fi
  if [ -e "$cache/libxcb-cursor.so.0" ]; then
    export LD_LIBRARY_PATH="$cache:${LD_LIBRARY_PATH:-}"
  fi
fi

# 3) Sanidade do display.
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
  echo "run-gui: nenhum DISPLAY/WAYLAND_DISPLAY — a GUI precisa de um servidor gráfico." >&2
fi

# 4) Lança a GUI (run.sh cuida do keyfile/binário).
exec "$here/run.sh" gui "$@"
