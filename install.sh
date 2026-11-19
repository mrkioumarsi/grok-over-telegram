#!/bin/bash
# Easy Ubuntu installer for GrokWire.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run: sudo bash install.sh"
  exit 1
fi
if ! grep -qi ubuntu /etc/os-release; then
  echo "This installer supports Ubuntu."
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
REPO="https://github.com/mrkioumarsi/grok-over-telegram.git"
SRC=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [ -z "$SRC" ] || [ ! -f "$SRC/bot.py" ] || [ ! -f "$SRC/jobrun.py" ]; then
  apt-get update
  apt-get install -y git ca-certificates
  dest="$(mktemp -d)"
  git clone --depth 1 "$REPO" "$dest/src"
  exec bash "$dest/src/install.sh"
fi
ROOT="/opt/grokwire"
BOX="/var/lib/grokwire"
WWW="/var/www/workspaces"

apt-get update
apt-get install -y python3 python3-venv python3-pip bubblewrap ca-certificates iptables

if ! getent group grokwire >/dev/null; then
  groupadd --system grokwire
fi
if ! id grokwire >/dev/null 2>&1; then
  useradd --system --gid grokwire --home-dir "$BOX" --shell /usr/sbin/nologin grokwire
fi

install -d -o root -g grokwire -m 750 "$BOX" "$BOX/auth" "$BOX/runtime"
install -d -o root -g grokwire -m 751 "$BOX/homes"
printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > "$BOX/resolv.conf"
chmod 644 "$BOX/resolv.conf"

install -d -o root -g root -m 700 "$ROOT" "$ROOT/data"
install -m 644 "$SRC/bot.py" "$ROOT/bot.py"
install -m 644 "$SRC/jobrun.py" "$ROOT/jobrun.py"
install -m 644 "$SRC/requirements.txt" "$ROOT/requirements.txt"
install -m 755 "$SRC/install.sh" "$ROOT/install.sh"
install -m 755 "$SRC/bin/grokwire" /usr/local/bin/grokwire
install -m 644 "$SRC/systemd/grokwire.service" /etc/systemd/system/grokwire.service

if [ ! -d "$ROOT/venv" ]; then
  python3 -m venv "$ROOT/venv"
fi
"$ROOT/venv/bin/pip" install --upgrade pip
"$ROOT/venv/bin/pip" install -r "$ROOT/requirements.txt"

if [ ! -f "$ROOT/.env" ]; then
  if [ -f /opt/grokbot/.env ]; then
    cp /opt/grokbot/.env "$ROOT/.env"
  elif [ -n "${BOT_TOKEN:-}" ] && [ -n "${ADMIN_ID:-}" ]; then
    umask 077
    printf 'BOT_TOKEN=%s\nADMIN_ID=%s\nGROK_BIN=/usr/local/bin/grok\n' "$BOT_TOKEN" "$ADMIN_ID" > "$ROOT/.env"
  else
    if [ ! -r /dev/tty ]; then
      echo "Set BOT_TOKEN and ADMIN_ID, then run the installer again."
      exit 1
    fi
    echo "Telegram bot token from BotFather:" >/dev/tty
    read -r BOT_TOKEN </dev/tty
    echo "Your numeric Telegram id (this becomes the admin):" >/dev/tty
    read -r ADMIN_ID </dev/tty
    umask 077
    printf 'BOT_TOKEN=%s\nADMIN_ID=%s\nGROK_BIN=/usr/local/bin/grok\n' "$BOT_TOKEN" "$ADMIN_ID" > "$ROOT/.env"
  fi
fi
chmod 600 "$ROOT/.env"

if [ ! -f "$ROOT/data/access.json" ] && [ -f /opt/grokbot/data/access.json ]; then
  cp /opt/grokbot/data/access.json "$ROOT/data/access.json"
  if [ -f /opt/grokbot/data/sessions.json ]; then
    cp /opt/grokbot/data/sessions.json "$ROOT/data/sessions.json"
  fi
  chmod 600 "$ROOT/data/"*.json || true
fi

install -d -o root -g root -m 711 "$WWW"
ADMIN_ID="$(grep '^ADMIN_ID=' "$ROOT/.env" | cut -d= -f2- | tr -d '[:space:]')"
for dir in "$WWW"/*; do
  [ -d "$dir" ] || continue
  base="$(basename "$dir")"
  if [ "$base" = "$ADMIN_ID" ]; then
    chown -R root:root "$dir"
    chmod 700 "$dir"
    continue
  fi
  name="gw${base}"
  if ! id "$name" >/dev/null 2>&1; then
    useradd --system --gid grokwire --no-create-home --shell /usr/sbin/nologin "$name"
  fi
  chown -R "$name:grokwire" "$dir"
  chmod 700 "$dir"
done

if [ -f /root/.grok/auth.json ]; then
  install -o root -g grokwire -m 640 /root/.grok/auth.json "$BOX/auth/auth.json"
  grok_real="$(readlink -f /usr/local/bin/grok || true)"
  if [ -n "$grok_real" ] && [ -f "$grok_real" ]; then
    install -o root -g root -m 755 "$grok_real" "$BOX/runtime/grok"
  fi
fi

grokwire firewall
systemctl daemon-reload
if systemctl list-unit-files | grep -q '^grokbot.service'; then
  systemctl disable --now grokbot || true
fi
systemctl enable --now grokwire
echo "Grok Over Telegram is installed. Open the menu with: grokwire"
echo "Sign in to Grok from the menu if this server is not signed in yet."
