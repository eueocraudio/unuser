#!/usr/bin/env bash
# Instalador do CLIENTE GUI do unuser numa máquina nova (Debian/Ubuntu).
#
# Baixa o código (repo público), monta o ambiente, instala a GUI (PySide6),
# configura a conexão e LANÇA a interface gráfica.
#
# Uso (rápido):
#   curl -fsSL https://raw.githubusercontent.com/eueocraudio/unuser/main/install-gui-client.sh | bash
#   # ou, com o arquivo em mãos:
#   bash install-gui-client.sh
#
# Variáveis de ambiente (todas opcionais):
#   UNUSER_SERVER       host:porta do servidor          [192.168.3.3:8080]
#   UNUSER_DEST         diretório de instalação          [$HOME/unuser]
#   UNUSER_REPO         URL do repositório git           [https://github.com/eueocraudio/unuser.git]
#   UNUSER_KEYFILE_SRC  origem do keyfile a COPIAR        (ex.: outra-maquina:~/.config/unuser/keyfile
#                       caminho local ou alvo scp)        — necessário p/ abrir um cofre EXISTENTE
#   UNUSER_NEW_VAULT=1  permite GERAR um keyfile novo     (só para começar um cofre do zero)
#   UNUSER_NO_LAUNCH=1  instala/config mas NÃO abre a GUI
#
# O keyfile é o 2º fator do cofre e NÃO está no repositório (é segredo). Para acessar um
# cofre que já existe, o MESMO keyfile da máquina original tem de ser copiado para cá —
# gerar um novo muda a chave-mestra e o cofre não abrirá.
set -euo pipefail

SERVER="${UNUSER_SERVER:-192.168.3.3:8080}"
DEST="${UNUSER_DEST:-$HOME/unuser}"
REPO="${UNUSER_REPO:-https://github.com/eueocraudio/unuser.git}"
HOST="${SERVER%%:*}"
PORT="${SERVER##*:}"
[ "$HOST" = "$PORT" ] && PORT=8080            # SERVER sem ':' → porta padrão
CONF_DIR="$HOME/.config/unuser"
KEYFILE="$CONF_DIR/keyfile"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[aviso]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[erro]\033[0m %s\n' "$*" >&2; exit 1; }

# --- 1) dependências de sistema (best-effort; pula se não houver apt/sudo) ----
if command -v apt-get >/dev/null; then
  PKGS="git python3 python3-venv python3-pip ca-certificates curl libxcb-cursor0 libxkbcommon0 libgl1"
  if [ "$(id -u)" = 0 ]; then SUDO=""; else SUDO="sudo"; fi
  say "Instalando dependências de sistema ($PKGS)…"
  if $SUDO -n true 2>/dev/null || [ -z "$SUDO" ]; then
    $SUDO apt-get update -qq && $SUDO apt-get install -y -qq $PKGS || \
      warn "apt falhou; sigo assim (o run-gui.sh resolve o libxcb sem root se faltar)."
  else
    warn "sudo exige senha (rode a parte de apt à mão se faltar algo):"
    warn "  sudo apt-get install -y $PKGS"
  fi
else
  warn "apt-get não encontrado — pulei dependências de sistema (precisa de git, python3>=3.11, venv, pip)."
fi
command -v git >/dev/null || die "git não encontrado e não consegui instalar. Instale e rode de novo."

# --- 2) baixa/atualiza o código ----------------------------------------------
if [ -d "$DEST/.git" ]; then
  say "Repositório já em $DEST — atualizando…"
  git -C "$DEST" pull --ff-only || warn "git pull falhou; usando o que já está lá."
else
  say "Clonando $REPO em $DEST…"
  git clone --depth 1 "$REPO" "$DEST"
fi
cd "$DEST"

# --- 3) venv (com fallback p/ ambientes sem ensurepip) -----------------------
if [ ! -x ".venv/bin/python" ]; then
  say "Criando ambiente virtual…"
  if ! python3 -m venv .venv 2>/dev/null; then
    warn "venv sem pip (sem ensurepip) — usando get-pip.py."
    python3 -m venv --without-pip .venv
    curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
    .venv/bin/python /tmp/get-pip.py
  fi
fi
say "Instalando o cliente + GUI (PySide6)… (pode demorar na 1ª vez)"
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -e ".[gui]"

# --- 4) configuração de conexão (não sobrescreve a existente) ----------------
mkdir -p "$CONF_DIR"
if [ ! -f "$HOME/.env" ]; then
  say "Escrevendo ~/.env (servidor $HOST:$PORT)…"
  cat > "$HOME/.env" <<EOF
# Conexão do cliente unuser (CHAVE=VALOR). Sem segredos (passphrase/keyfile ficam fora).
UNUSER_VAULT_ID=v:default
UNUSER_CONN_MODE=direct
UNUSER_DIRECT_HOST=$HOST
UNUSER_DIRECT_PORT=$PORT
UNUSER_TOR_ONION=
UNUSER_TOR_PORT=$PORT
UNUSER_TOR_SOCKS=127.0.0.1:9050
EOF
else
  warn "~/.env já existe — mantido como está (não sobrescrevi)."
fi
if [ ! -f "$CONF_DIR/dirs.json" ]; then
  say "Criando dirs.json (sincroniza ~/Documents por padrão)…"
  cat > "$CONF_DIR/dirs.json" <<EOF
{
  "default_dirs": [ { "path": "$HOME/Documents", "recursive": true } ],
  "extra_items": []
}
EOF
fi

# --- 5) keyfile (2º fator) — a armadilha do cofre ----------------------------
if [ ! -f "$KEYFILE" ]; then
  if [ -n "${UNUSER_KEYFILE_SRC:-}" ]; then
    say "Copiando keyfile de $UNUSER_KEYFILE_SRC…"
    case "$UNUSER_KEYFILE_SRC" in
      *:*) scp "$UNUSER_KEYFILE_SRC" "$KEYFILE" ;;   # alvo scp (host:caminho)
      *)   cp "$UNUSER_KEYFILE_SRC" "$KEYFILE" ;;     # caminho local
    esac
    chmod 600 "$KEYFILE"
  elif [ "${UNUSER_NEW_VAULT:-}" = 1 ]; then
    warn "Gerando keyfile NOVO (cofre do zero). As OUTRAS máquinas terão de usar ESTE mesmo arquivo:"
    warn "  $KEYFILE"
    head -c 64 /dev/urandom > "$KEYFILE"
    chmod 600 "$KEYFILE"
  else
    die "Falta o keyfile do cofre ($KEYFILE).
     Para acessar o cofre EXISTENTE, copie o MESMO keyfile da máquina original:
       UNUSER_KEYFILE_SRC=usuario@maquina-original:~/.config/unuser/keyfile bash $0
     Para começar um cofre NOVO (e usar este keyfile nas demais máquinas):
       UNUSER_NEW_VAULT=1 bash $0"
  fi
fi

say "Pronto. Cliente instalado em $DEST, apontando para $HOST:$PORT."

# --- 6) lança a GUI ----------------------------------------------------------
if [ "${UNUSER_NO_LAUNCH:-}" = 1 ]; then
  say "UNUSER_NO_LAUNCH=1 — não abri a GUI. Para abrir depois:"
  say "  cd $DEST && src/client/run-gui.sh"
  exit 0
fi
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
  warn "Sem DISPLAY/WAYLAND — não há servidor gráfico. Abra num ambiente com tela:"
  warn "  cd $DEST && src/client/run-gui.sh"
  exit 0
fi
say "Abrindo a GUI (vai pedir a passphrase do cofre)…"
exec src/client/run-gui.sh
