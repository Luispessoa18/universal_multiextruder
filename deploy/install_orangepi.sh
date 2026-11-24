#!/usr/bin/env bash
# Instala a interface web como serviço no Orange Pi (Armbian/Debian/Ubuntu).
# Rode como usuário normal (sem sudo): bash deploy/install_orangepi.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip

python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install --upgrade pip
"$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt"

# Acesso às portas seriais USB (/dev/ttyUSB*, /dev/ttyACM*)
sudo usermod -aG dialout "$USER_NAME"

# brltty costuma "roubar" adaptadores CH340 (Vyper e Arduinos clones)
if dpkg -s brltty >/dev/null 2>&1; then
  echo "Removendo brltty (conflita com CH340)..."
  sudo apt-get remove -y brltty
fi

if [ ! -f "$DIR/.env" ]; then
  PASS="$(python3 -c 'import secrets;print(secrets.token_urlsafe(12))')"
  printf 'MMU_USER=admin\nMMU_PASSWORD=%s\nMMU_HOST=0.0.0.0\nMMU_PORT=8080\n' "$PASS" > "$DIR/.env"
  chmod 600 "$DIR/.env"
  echo "Senha gerada em $DIR/.env -> usuario: admin  senha: $PASS"
fi

sed -e "s|__DIR__|$DIR|g" -e "s|__USER__|$USER_NAME|g" "$DIR/deploy/mmu-web.service" \
  | sudo tee /etc/systemd/system/mmu-web.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now mmu-web
sudo systemctl restart mmu-web

echo
echo "Pronto. Acesse http://$(hostname -I | awk '{print $1}'):8080"
echo "Logs: journalctl -u mmu-web -f"
