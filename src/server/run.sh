#!/usr/bin/env bash
# Sobe o servidor cofre-cego (unuserd) com defaults sensatos.
#
# Override por variáveis de ambiente; argumentos extras vão direto ao unuserd:
#   UNUSERD_STORAGE   diretório do cofre (blobs + manifesto)  [~/.local/share/unuser-vault]
#   UNUSERD_HOST      endereço de escuta                      [127.0.0.1]
#   UNUSERD_PORT      porta                                   [8080]
#   UNUSERD_TLS_CERT / UNUSERD_TLS_KEY / UNUSERD_TLS_ALLOW    liga mTLS se os 3 existirem
#
# Exemplos:
#   src/server/run.sh
#   UNUSERD_PORT=9000 src/server/run.sh
#   UNUSERD_TLS_CERT=s.crt UNUSERD_TLS_KEY=s.key UNUSERD_TLS_ALLOW=allow.pem src/server/run.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"

storage="${UNUSERD_STORAGE:-$HOME/.local/share/unuser-vault}"
host="${UNUSERD_HOST:-127.0.0.1}"
port="${UNUSERD_PORT:-8080}"

# unuserd do venv do projeto, se houver; senão o do PATH (ex.: instalado via .deb).
if [ -x "$root/.venv/bin/unuserd" ]; then
  bin="$root/.venv/bin/unuserd"
else
  bin="unuserd"
fi

args=(--storage "$storage" --host "$host" --port "$port")

# mTLS é opcional: só liga quando os três caminhos forem fornecidos.
if [ -n "${UNUSERD_TLS_CERT:-}" ] && [ -n "${UNUSERD_TLS_KEY:-}" ] && [ -n "${UNUSERD_TLS_ALLOW:-}" ]; then
  args+=(--tls-cert "$UNUSERD_TLS_CERT" --tls-key "$UNUSERD_TLS_KEY" --tls-allow "$UNUSERD_TLS_ALLOW")
  echo "unuserd: mTLS ligado" >&2
else
  echo "unuserd: SEM TLS (ok só em localhost; use Tor+mTLS entre máquinas)" >&2
fi

mkdir -p "$storage"
echo "unuserd: cofre em $storage, escutando $host:$port" >&2
exec "$bin" "${args[@]}" "$@"
