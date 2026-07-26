#!/usr/bin/env bash
# fleetmem-init-pki.sh — generate fleetmem's OWN local mTLS CA + brain server cert (run once at install).
#
# Self-contained: plain openssl, no external or cloud PKI required. The local CA signs the brain
# SERVER cert (TLS on the mesh listener) and, via sign-agent-cert.sh, every AGENT CLIENT cert.
# Private keys are generated here and NEVER leave this host. Re-running is safe: existing files are
# kept (delete $PKI_DIR to regenerate). NOTHING here ships pre-made — a fresh box mints its own.
#
# If you already run your own PKI, skip this and drop your ca.crt/server.{crt,key} into $PKI_DIR.
#
# Usage:  BRAIN_HOST=brain.example.com [BRAIN_IP=10.0.0.5] ./fleetmem-init-pki.sh
set -euo pipefail

PKI_DIR="${PKI_DIR:-/opt/brain-db/pki}"
BRAIN_HOST="${BRAIN_HOST:-$(hostname -f 2>/dev/null || hostname)}"
BRAIN_IP="${BRAIN_IP:-}"
CA_DAYS="${CA_DAYS:-3650}"
SRV_DAYS="${SRV_DAYS:-825}"

mkdir -p "$PKI_DIR"; chmod 700 "$PKI_DIR"; cd "$PKI_DIR"

# 1) local CA (self-signed) -------------------------------------------------
if [ ! -f ca.crt ]; then
  openssl genrsa -out ca.key 4096; chmod 600 ca.key
  # basicConstraints CA:TRUE + keyUsage keyCertSign,cRLSign are REQUIRED: modern OpenSSL (3.x) and
  # Python 3.13 reject a signing CA that lacks them, so agents can't complete the mTLS handshake.
  openssl req -x509 -new -nodes -key ca.key -sha256 -days "$CA_DAYS" \
    -subj "/CN=fleetmem Local CA/O=fleetmem" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -out ca.crt
  echo "created local CA -> $PKI_DIR/ca.crt"
else
  echo "CA exists, keeping -> $PKI_DIR/ca.crt"
fi

# 2) brain server cert (signed by the local CA) -----------------------------
if [ ! -f server.crt ]; then
  SAN="DNS:${BRAIN_HOST}"; [ -n "$BRAIN_IP" ] && SAN="${SAN},IP:${BRAIN_IP}"
  openssl genrsa -out server.key 2048; chmod 600 server.key
  openssl req -new -key server.key -subj "/CN=${BRAIN_HOST}/O=fleetmem" -out server.csr
  openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -days "$SRV_DAYS" -sha256 \
    -extfile <(printf "subjectAltName=%s\nextendedKeyUsage=serverAuth\n" "$SAN") \
    -out server.leaf
  cat server.leaf ca.crt > server.crt      # fullchain for nginx
  rm -f server.csr server.leaf
  echo "created brain server cert (CN=${BRAIN_HOST}, SAN=${SAN})"
else
  echo "server cert exists, keeping -> $PKI_DIR/server.crt"
fi

echo
echo "PKI ready in $PKI_DIR. nginx wants:"
echo "  ssl_certificate        $PKI_DIR/server.crt"
echo "  ssl_certificate_key    $PKI_DIR/server.key"
echo "  ssl_client_certificate $PKI_DIR/ca.crt"
echo "Distribute ca.crt (public) to each agent as its CA bundle. Sign agent certs with sign-agent-cert.sh."
