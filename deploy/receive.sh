#!/bin/bash
# The only thing the deploy key is allowed to run.
#
# The key in ~/.ssh/authorized_keys carries command="…/receive.sh", so a
# connection made with it cannot open a shell, forward a port or run
# anything else - it can only hand this script a tarball on stdin. That
# matters more here than on a machine of one's own: this box also serves
# an unrelated production site, and a plain deploy key in a GitHub secret
# is a root shell on it for anybody who can read the repository or a
# workflow log.
#
# Install (once, as root on the server):
#   command="/opt/icp-scout/deploy/receive.sh",no-agent-forwarding,\
#   no-port-forwarding,no-pty,no-X11-forwarding ssh-ed25519 AAAA… deploy
#
# What arrives is a gzipped tar of the repository's tracked files. What
# is deliberately NOT in it, and must never be overwritten from git:
#
#   .env           the tunnel token and the API key
#   data/          two gigabytes of register data and the evidence archive
#   web/icp.json   the brief the salesperson saved through the interface
#
# The first two are not in the repository at all. The third is, which is
# why it is excluded by name below rather than by hoping.

set -euo pipefail

ROOT=/opt/icp-scout
STAGING=$(mktemp -d)
trap 'rm -rf "$STAGING"' EXIT

say() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*"; }

say "receiving"
# --no-same-owner: the sender is a Windows checkout in CI, and its uid is
# meaningless here. Ownership is set below, once, to what the container
# runs as.
tar xzf - --no-same-owner -C "$STAGING"

if [ ! -f "$STAGING/docker-compose.yml" ] || [ ! -d "$STAGING/pipeline" ]; then
	say "refusing: the archive does not look like icp-scout"
	exit 1
fi

# web/icp.json is tracked, so it arrives in the tarball and would replace
# whatever brief was last saved through the interface. Dropping it here
# leaves the live one alone.
rm -f "$STAGING/web/icp.json" "$STAGING/web/icp.json.bak"

say "syncing into $ROOT"
# --delete so a file removed in git is removed here too; a page that was
# renamed would otherwise stay served from its old path forever. Only the
# directories git actually owns are deleted from - data/ and .env sit
# outside them.
#
# Two things --delete has already broken once, both worth keeping in
# mind. This script lives in deploy/ and therefore rsyncs over itself
# while it is running: that is safe only because rsync writes a temp file
# and renames it, so the shell keeps reading the inode it opened. And it
# deleted itself outright the first time it ran, because the file existed
# on the server but had never been committed - anything this needs must
# be in git, or --delete is right to remove it.
rsync -a --delete \
	"$STAGING/pipeline/" "$ROOT/pipeline/"
rsync -a --delete \
	"$STAGING/api/" "$ROOT/api/"
rsync -a --delete --exclude=icp.json --exclude=icp.json.bak \
	"$STAGING/web/" "$ROOT/web/"
rsync -a --delete \
	"$STAGING/deploy/" "$ROOT/deploy/"
for file in docker-compose.yml Dockerfile requirements.txt .dockerignore DEPLOY.md; do
	[ -f "$STAGING/$file" ] && rsync -a "$STAGING/$file" "$ROOT/$file"
done

# The bind mounts are written by uid 1000 inside the container, which is
# not root; a file that arrives owned by root is a file the app cannot
# rewrite. web/ is the one that matters - POST /api/icp writes into it.
chown -R 1000:1000 "$ROOT/web"
chmod +x "$ROOT/deploy/receive.sh"

cd "$ROOT"

say "building"
docker compose build app

say "restarting"
docker compose up -d app

# The image carries the code; web/ and data/ are bind mounts, so the
# interface is already live at this point and only the Python needed the
# restart.
say "waiting for health"
for _ in $(seq 1 30); do
	state=$(docker inspect --format '{{.State.Health.Status}}' icp-scout-app-1 2>/dev/null || echo unknown)
	[ "$state" = "healthy" ] && break
	sleep 2
done
say "health: ${state:-unknown}"
[ "${state:-unknown}" = "healthy" ] || { docker compose logs --tail 40 app; exit 1; }

# Through Caddy, not through the app's own port: this is the path a
# visitor takes, and a Caddyfile that stopped routing would otherwise
# pass a deploy that serves nothing.
say "smoke test through caddy"
docker compose exec -T app python - <<'PY'
import json, sys, urllib.request

failures = []
for path in ("/", "/brief/", "/history/", "/api/run", "/api/results", "/api/history"):
    try:
        response = urllib.request.urlopen("http://caddy:80" + path, timeout=30)
        body = response.read()
        print(f"  {path:16} {response.status}  {len(body)} b")
        if response.status != 200:
            failures.append(path)
    except Exception as error:                      # noqa: BLE001 - report, do not classify
        print(f"  {path:16} FAILED {error}")
        failures.append(path)

# One card, because the endpoint that broke in the past broke silently:
# it answered 404 for every company while every page above stayed 200.
try:
    week = json.loads(urllib.request.urlopen("http://caddy:80/api/results", timeout=30).read())
    top = week.get("top") or []
    if top:
        ico = top[0]["ico"]
        status = urllib.request.urlopen(f"http://caddy:80/api/card/{ico}", timeout=90).status
        print(f"  /api/card/{ico}  {status}")
        if status != 200:
            failures.append("card")
    else:
        print("  no week to check a card against - skipped")
except Exception as error:                          # noqa: BLE001
    print(f"  card check FAILED {error}")
    failures.append("card")

sys.exit(1 if failures else 0)
PY

say "deployed"
