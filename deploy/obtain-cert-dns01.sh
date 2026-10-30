#!/usr/bin/env bash
# Renew edutest.narxoz.kz via DNS-01. Add the printed TXT, then this script finishes.
# Run: sudo bash /home/administrator/edutest_BE/deploy/obtain-cert-dns01.sh
set -euo pipefail
if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash $0"
  exit 1
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="${ROOT}/dns01-auth-hook.sh"
chmod +x "$HOOK"

certbot certonly \
  --manual \
  --preferred-challenges dns \
  --manual-auth-hook "${HOOK}" \
  --manual-cleanup-hook /bin/true \
  --cert-name edutest.narxoz.kz \
  -d edutest.narxoz.kz \
  --agree-tos \
  --non-interactive \
  --force-renewal \
  "$@"

nginx -t
systemctl reload nginx
echo "edutest.narxoz.kz certificate renewed and nginx reloaded"
