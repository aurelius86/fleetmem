#!/usr/bin/env bash
# sign-agent-cert.sh <cn> <csr-file> [days]  — sign an agent's client CSR with the local fleetmem CA.
#
# The brain API issues an enrolled agent's token + identity but DELIBERATELY holds NO CA power, so a
# manager signs the approved applicant's CSR here with the local CA (see fleetmem-init-pki.sh). This
# keeps cert-minting off the network service: an API compromise can never issue certs. The agent's
# PRIVATE KEY never leaves the agent — only the public CSR is handed over, only the public cert
# comes back. <cn> = the agent name the user chose; it must match the CSR's CN and the agent row.
#
# Usage:  ./sign-agent-cert.sh <name> ./<name>.csr [days]
# Output: <cn>-client.crt  (leaf + local-CA chain). Give it to the agent alongside ca.crt.
set -euo pipefail

CN="${1:?usage: sign-agent-cert.sh <cn> <csr-file> [days]}"
CSR="${2:?usage: sign-agent-cert.sh <cn> <csr-file> [days]}"
DAYS="${3:-90}"
PKI_DIR="${PKI_DIR:-/opt/brain-db/pki}"
OUT="$(dirname "$CSR")/${CN}-client.crt"

[ -f "$CSR" ] || { echo "sign FAILED: CSR '$CSR' not found" >&2; exit 1; }
[ -f "$PKI_DIR/ca.crt" ] && [ -f "$PKI_DIR/ca.key" ] || {
  echo "sign FAILED: local CA not found in $PKI_DIR — run fleetmem-init-pki.sh first" >&2; exit 1; }

# sanity: the CSR's CN must equal the requested agent name (the brain matches cert_cn==CN==agent)
CSR_CN=$(openssl req -in "$CSR" -noout -subject 2>/dev/null | sed -n 's/.*CN *= *\([^,/]*\).*/\1/p' | tr -d ' ')
[ "$CSR_CN" = "$CN" ] || { echo "sign FAILED: CSR CN '$CSR_CN' != requested '$CN'" >&2; exit 1; }

openssl x509 -req -in "$CSR" -CA "$PKI_DIR/ca.crt" -CAkey "$PKI_DIR/ca.key" -CAcreateserial \
  -days "$DAYS" -sha256 -extfile <(printf "extendedKeyUsage=clientAuth\n") -out "$OUT.leaf"
cat "$OUT.leaf" "$PKI_DIR/ca.crt" > "$OUT"; rm -f "$OUT.leaf"

if [ -s "$OUT" ] && openssl x509 -in "$OUT" -noout -subject >/dev/null 2>&1; then
  echo "SIGNED -> $OUT"
  openssl x509 -in "$OUT" -noout -subject -issuer -enddate
  echo "chain certs: $(grep -c 'BEGIN CERTIFICATE' "$OUT")  (expect 2: leaf + local CA)"
else
  echo "sign FAILED; removing empty output" >&2; rm -f "$OUT"; exit 1
fi
