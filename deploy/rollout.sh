#!/bin/sh
# Staggered rolling restart of the app replicas.
#
# `docker compose restart app` stops every replica at once, which is exactly the 1x502 this exists
# to prevent — measured on a real deploy, with health checks and Caddy retry already in place. There
# was simply no instance left to serve.
#
# This restarts one replica at a time and waits for it to report READY (not merely running) before
# touching the next, so at least one healthy instance is always behind the load balancer.
#
#   ./deploy/rollout.sh            # rolling restart of the current image
#   ./deploy/rollout.sh --build    # rebuild first (this stack needs `build`, not just `up -d`)
set -eu

COMPOSE="docker compose -f $(dirname "$0")/docker-compose.prod.yml"
SERVICE=app
REPLICAS=${REPLICAS:-2}
WAIT_SECONDS=${WAIT_SECONDS:-90}

if [ "${1:-}" = "--build" ]; then
  echo "[rollout] building..."
  $COMPOSE build "$SERVICE"
fi

wait_healthy() {
  container="$1"
  i=0
  while [ "$i" -lt "$WAIT_SECONDS" ]; do
    state=$(docker inspect -f '{{.State.Health.Status}}' "$container" 2>/dev/null || echo starting)
    if [ "$state" = "healthy" ]; then
      echo "[rollout] $container healthy"
      return 0
    fi
    i=$((i + 2))
    sleep 2
  done
  echo "[rollout] ERROR: $container did not become healthy in ${WAIT_SECONDS}s" >&2
  return 1
}

echo "[rollout] ensuring $REPLICAS replicas of $SERVICE"
$COMPOSE up -d --scale "$SERVICE=$REPLICAS" --no-recreate "$SERVICE"

containers=$($COMPOSE ps -q "$SERVICE")
[ -n "$containers" ] || { echo "[rollout] no $SERVICE containers found" >&2; exit 1; }

for c in $containers; do
  echo "[rollout] restarting $c"
  docker restart "$c" >/dev/null
  # Fail the whole rollout rather than continue: taking down the second replica when the first has
  # not come back is how a rolling restart becomes an outage.
  wait_healthy "$c" || {
    echo "[rollout] ABORTED — remaining replicas left untouched and still serving" >&2
    exit 1
  }
done

echo "[rollout] done — all replicas restarted with no full-stop window"
echo "[rollout] rollback: git checkout <previous-sha> && ./deploy/rollout.sh --build"
