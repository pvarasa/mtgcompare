#!/usr/bin/env bash
# Build the current working tree, push as ghcr.io/pvarasa/mtgcompare:dev,
# and roll the in-cluster stg deployment so kubelet pulls the new layer.
#
# Skips the GitHub release pipeline entirely — this is the fast path
# for testing changes against the prod Postgres + cache from
# https://mtg-stg.vpablo.dev (gated by the Cloudflare Access admin
# perimeter; see ../server_admin/terraform/cloudflare/variables.tf
# apps.mtgcompare_stg).
#
# Prerequisites (one-time):
#   * Docker / Docker Desktop running, with the engine reachable
#     (`docker info` succeeds).
#   * Logged in to GHCR with a PAT that has write:packages scope:
#       echo $GHCR_PAT | docker login ghcr.io -u pvarasa --password-stdin
#   * kubectl pointed at vps1 (SSH tunnel up: ../server_admin/tunnel.sh).
#
# Usage:
#   ./scripts/dev-push.sh            # build + push + restart
#   ./scripts/dev-push.sh --no-restart
#                                    # build + push, skip kubectl rollout
set -euo pipefail

IMAGE="ghcr.io/pvarasa/mtgcompare:dev"
NS="apps"
DEPLOY="mtgcompare-stg"

restart=true
for arg in "$@"; do
    case "$arg" in
        --no-restart) restart=false ;;
        *)
            echo "unknown arg: $arg" >&2
            exit 2
            ;;
    esac
done

cd "$(dirname "$0")/.."

echo "▸ building $IMAGE"
docker build -t "$IMAGE" .

echo "▸ pushing $IMAGE"
docker push "$IMAGE"

if [ "$restart" = "true" ]; then
    echo "▸ rollout restart $NS/$DEPLOY"
    kubectl rollout restart -n "$NS" deployment/"$DEPLOY"
    kubectl rollout status  -n "$NS" deployment/"$DEPLOY" --timeout=180s
    echo
    echo "deployed → https://mtg-stg.vpablo.dev"
else
    echo "skipped kubectl rollout — push only"
fi
