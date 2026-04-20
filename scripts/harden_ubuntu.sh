#!/usr/bin/env bash
set -euo pipefail

SSHD_CONFIG="/etc/ssh/sshd_config"

if grep -qE '^\s*PermitRootLogin' "$SSHD_CONFIG"; then
  sed -i 's/^\s*PermitRootLogin.*/PermitRootLogin no/' "$SSHD_CONFIG"
else
  echo 'PermitRootLogin no' >> "$SSHD_CONFIG"
fi

if grep -qE '^\s*PasswordAuthentication' "$SSHD_CONFIG"; then
  sed -i 's/^\s*PasswordAuthentication.*/PasswordAuthentication no/' "$SSHD_CONFIG"
else
  echo 'PasswordAuthentication no' >> "$SSHD_CONFIG"
fi

systemctl reload ssh || systemctl reload sshd || true

ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

