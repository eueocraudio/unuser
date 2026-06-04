#!/usr/bin/env bash
# Wrapper do cliente unuser: garante o keyfile (2º fator) e repassa o subcomando.
#
# Variáveis de ambiente:
#   UNUSER_KEYFILE     caminho do keyfile (2º fator)   [~/.config/unuser/keyfile]
#   UNUSER_PASSPHRASE  passphrase (opcional; sem ela, pede no prompt)
#
# Subcomandos (repassados ao unuser): status | send | receive | delete | gui
#
# Exemplos:
#   src/client/run.sh status
#   src/client/run.sh send
#   src/client/run.sh receive Documents/teste.txt
#   src/client/run.sh gui
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"

# unuser do venv do projeto, se houver; senão o do PATH (ex.: instalado via .deb).
if [ -x "$root/.venv/bin/unuser" ]; then
  bin="$root/.venv/bin/unuser"
else
  bin="unuser"
fi

export UNUSER_KEYFILE="${UNUSER_KEYFILE:-$HOME/.config/unuser/keyfile}"

# Gera um keyfile de alta entropia (0600) na primeira execução, se não existir.
# ATENÇÃO: este é um SEGREDO do cofre. Ele NÃO vai ao servidor. Em outras máquinas,
# COPIE este mesmo arquivo — não gere um novo, senão o cofre não abrirá lá.
if [ ! -f "$UNUSER_KEYFILE" ]; then
  mkdir -p "$(dirname "$UNUSER_KEYFILE")"
  head -c 64 /dev/urandom > "$UNUSER_KEYFILE"
  chmod 600 "$UNUSER_KEYFILE"
  echo "unuser: keyfile gerado em $UNUSER_KEYFILE" >&2
  echo "unuser: GUARDE-O e copie o MESMO arquivo para as outras máquinas (perdê-lo = perder o cofre)" >&2
fi

if [ "$#" -eq 0 ]; then
  echo "uso: $(basename "$0") {status|send|receive|delete|gui} [args...]" >&2
  exit 2
fi

exec "$bin" "$@"
