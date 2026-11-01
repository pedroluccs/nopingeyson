#!/usr/bin/env bash
# Instala o relay do NoPingeyson numa VPS Linux (Debian/Ubuntu) como serviço systemd.
#
#   1. Copie a pasta "nopingeyson" e este script para a VPS:
#        scp -r nopingeyson deploy/install-relay.sh root@IP_DA_VPS:/root/
#   2. Na VPS:
#        sudo bash install-relay.sh <CHAVE_HEX> [PORTA]
set -euo pipefail

KEY="${1:?uso: install-relay.sh <CHAVE_HEX> [PORTA]}"
PORT="${2:-7000}"
DIR=/opt/nopingeyson

command -v python3 >/dev/null || { apt-get update && apt-get install -y python3; }
command -v ping >/dev/null || apt-get install -y iputils-ping

mkdir -p "$DIR"
cp -r "$(dirname "$0")/nopingeyson" "$DIR/" 2>/dev/null || cp -r ./nopingeyson "$DIR/"
id nopingeyson >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin nopingeyson

cat > /etc/nopingeyson-relay.env <<EOF
NOPINGEYSON_KEY=$KEY
EOF
chmod 600 /etc/nopingeyson-relay.env

cat > /etc/systemd/system/nopingeyson-relay.service <<EOF
[Unit]
Description=NoPingeyson relay UDP
After=network-online.target
Wants=network-online.target

[Service]
User=nopingeyson
EnvironmentFile=/etc/nopingeyson-relay.env
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 -m nopingeyson.relay --port $PORT
Restart=always
RestartSec=2
# buffers UDP maiores ajudam em rajadas
LimitNOFILE=65536
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF

# buffers de socket maiores (o relay pede 4 MB)
sysctl -w net.core.rmem_max=8388608 net.core.wmem_max=8388608 >/dev/null
grep -q nopingeyson /etc/sysctl.conf || printf '# nopingeyson\nnet.core.rmem_max=8388608\nnet.core.wmem_max=8388608\n' >> /etc/sysctl.conf

# libera a porta no firewall, se houver ufw
if command -v ufw >/dev/null && ufw status | grep -q active; then ufw allow "$PORT"/udp; fi

systemctl daemon-reload
systemctl enable --now nopingeyson-relay
sleep 1
systemctl --no-pager status nopingeyson-relay | head -n 5
echo "Relay rodando na porta $PORT/udp. Libere essa porta também no painel do provedor da VPS."
