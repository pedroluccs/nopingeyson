#!/usr/bin/env bash
# Instala o relay do NoPingeyson numa VPS Linux com um comando só:
#
#   curl -fsSL https://raw.githubusercontent.com/pedroluccs/nopingeyson/main/deploy/bootstrap.sh | sudo bash -s -- CHAVE_HEX [PORTA]
#
# Baixa o código do GitHub, instala como serviço (systemd) e libera a porta UDP.
set -euo pipefail

KEY="${1:?uso: bootstrap.sh CHAVE_HEX [PORTA]}"
PORT="${2:-7000}"
REPO="https://github.com/pedroluccs/nopingeyson/archive/refs/heads/main.tar.gz"

command -v curl >/dev/null || { apt-get update && apt-get install -y curl; }
TMP=$(mktemp -d)
curl -fsSL "$REPO" | tar -xz -C "$TMP"
cd "$TMP"/nopingeyson-main
bash deploy/install-relay.sh "$KEY" "$PORT"

IP=$(curl -fsS4 https://api.ipify.org || hostname -I | awk '{print $1}')
echo
echo "==============================================="
echo " Relay do NoPingeyson pronto"
echo "   IP:    $IP"
echo "   Porta: $PORT/udp"
echo " Mande esse IP para configurar o NoPingeyson."
echo "==============================================="
