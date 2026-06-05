#!/usr/bin/env bash
# Idempotently add note.danielteshome.dev to the host cloudflared tunnel config.
#
#   sudo bash deploy/add-tunnel-route.sh
#
# Backs the config up first, inserts the route before the http_status:404
# catch-all (no-op if it's already there), validates, then prints the two
# remaining commands (DNS route + restart) for you to run after eyeballing it.
set -euo pipefail

CONFIG="${CONFIG:-/etc/cloudflared/config.yml}"
HOST="${HOST:-note.danielteshome.dev}"
NODEPORT="${NODEPORT:-30568}"
TUNNEL="${TUNNEL:-d2d9aa1c-74d2-4945-9973-72c35104561e}"

[ -w "$CONFIG" ] || { echo "Cannot write $CONFIG — run with sudo."; exit 1; }

BACKUP="${CONFIG}.bak.$(date +%Y%m%d-%H%M%S)"
cp -a "$CONFIG" "$BACKUP"
echo "Backed up -> $BACKUP"

CONFIG="$CONFIG" HOST="$HOST" NODEPORT="$NODEPORT" python3 - <<'PY'
import os, re, sys
path, host, port = os.environ["CONFIG"], os.environ["HOST"], os.environ["NODEPORT"]
s = open(path).read()
if host in s:
    print(f"{host} already present — no change."); sys.exit(0)
m = re.search(r"^[ \t]*- *service: *http_status:404.*$", s, re.M)
if not m:
    print("ERROR: '- service: http_status:404' catch-all not found; aborting."); sys.exit(1)
block = (f"  - hostname: {host}\n"
         f"    service: http://localhost:{port}\n"
         f"    originRequest:\n"
         f"      httpHostHeader: {host}\n"
         f"      noTLSVerify: true\n\n")
open(path, "w").write(s[:m.start()] + block + s[m.start():])
print(f"Inserted: {host} -> http://localhost:{port}")
PY

if command -v cloudflared >/dev/null; then
  cloudflared --config "$CONFIG" tunnel ingress validate \
    && echo "cloudflared: ingress config valid" \
    || echo "WARNING: cloudflared validate reported an issue — review $CONFIG (backup at $BACKUP)"
fi

echo
echo "Now run these two, then check https://$HOST/healthz :"
echo "  sudo cloudflared tunnel route dns $TUNNEL $HOST"
echo "  sudo systemctl restart cloudflared"
