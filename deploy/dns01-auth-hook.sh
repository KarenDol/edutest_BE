#!/usr/bin/env bash
# Certbot DNS-01 hook: print TXT and wait until all ps.kz nameservers see it.
set -euo pipefail
name="_acme-challenge.${CERTBOT_DOMAIN}"
val="${CERTBOT_VALIDATION}"
host="_acme-challenge.${CERTBOT_DOMAIN%.narxoz.kz}"
out="/home/administrator/edutest_BE/deploy/acme-txt.txt"

{
  echo "name=${name}"
  echo "host=${host}"
  echo "zone=narxoz.kz"
  echo "type=TXT"
  echo "value=${val}"
} > "${out}"

echo "========================================"
echo "Add TXT record in DNS (ps.kz / narxoz.kz):"
echo "  Zone:  narxoz.kz"
echo "  Host:  ${host}"
echo "  FQDN:  ${name}"
echo "  Type:  TXT"
echo "  Value: ${val}"
echo "========================================"

ok_txt() {
  local server="$1"
  dig +bufsize=4096 +short TXT "${name}" "@${server}" | tr -d '"' | grep -qF "${val}"
}

for i in $(seq 1 180); do
  ns1_ok=0 ns2_ok=0 ns3_ok=0
  ok_txt ns1.ps.kz && ns1_ok=1
  ok_txt ns2.ps.kz && ns2_ok=1
  ok_txt ns3.ps.kz && ns3_ok=1
  echo "try ${i}: ns1=${ns1_ok} ns2=${ns2_ok} ns3=${ns3_ok}"
  if [[ "${ns1_ok}" -eq 1 && "${ns2_ok}" -eq 1 && "${ns3_ok}" -eq 1 ]]; then
    echo "OK: TXT visible on ns1/ns2/ns3 for ${name}"
    sleep 20
    exit 0
  fi
  sleep 10
done
echo "TIMEOUT: TXT for ${name} not found on all ns1/ns2/ns3.ps.kz"
exit 1
