#!/usr/bin/env bash
# Measures RTO for two chaos scenarios:
#   1. Single pod forced-delete
#   2. Node cordon + drain
# Targets: pod failure ≤30s, node failure ≤90s

set -euo pipefail

NAMESPACE="guardian"
DEPLOYMENT="dashboard-api"
TARGET_POD_RTO_S=30
TARGET_NODE_RTO_S=90

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${YELLOW}[$(date '+%H:%M:%S.%3N')]${NC} $1"; }
pass() { echo -e "${GREEN}  ✓ PASS${NC}: $1"; }
fail() { echo -e "${RED}  ✗ FAIL${NC}: $1"; }
sep()  { echo -e "${CYAN}══════════════════════════════════════════════${NC}"; }

# ─── helpers ──────────────────────────────────────────────────────────────────

get_ready() {
  kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" \
    -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0"
}

wait_until_ready() {
  local target=$1
  while true; do
    local ready
    ready=$(get_ready)
    [[ "${ready:-0}" -ge "$target" ]] && return 0
    sleep 0.5
  done
}

rto_ms_to_s() { echo "scale=2; $1 / 1000" | bc; }

# ─── Scenario 1: Pod failure ──────────────────────────────────────────────────
sep
echo "  Scenario 1 — Forced pod deletion"
sep

POD=$(kubectl get pods -n "$NAMESPACE" -l "app=$DEPLOYMENT" \
  --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].metadata.name}')
log "Target pod  : $POD"

INITIAL_READY=$(get_ready)
log "Ready before: $INITIAL_READY"

S1_START=$(date +%s%N)
log "Deleting pod …"
kubectl delete pod "$POD" -n "$NAMESPACE" --grace-period=0 --force 2>/dev/null || true

wait_until_ready "$INITIAL_READY"
S1_END=$(date +%s%N)

S1_MS=$(( (S1_END - S1_START) / 1000000 ))
S1_S=$(rto_ms_to_s "$S1_MS")
log "Pod failure RTO: ${S1_S}s  (${S1_MS}ms)"

if (( S1_MS <= TARGET_POD_RTO_S * 1000 )); then
  pass "Pod RTO ${S1_S}s ≤ ${TARGET_POD_RTO_S}s target"
else
  fail "Pod RTO ${S1_S}s > ${TARGET_POD_RTO_S}s target"
fi

sleep 5  # let the cluster settle

# ─── Scenario 2: Node drain ───────────────────────────────────────────────────
sep
echo "  Scenario 2 — Node cordon + drain"
sep

# Pick the node hosting the most dashboard-api pods
NODE=$(kubectl get pods -n "$NAMESPACE" -l "app=$DEPLOYMENT" \
  -o wide --no-headers | awk '{print $7}' | sort | uniq -c | sort -rn | head -1 | awk '{print $2}')
log "Target node : $NODE"

INITIAL_READY=$(get_ready)
log "Ready before: $INITIAL_READY"

S2_START=$(date +%s%N)
log "Cordoning $NODE …"
kubectl cordon "$NODE"

log "Draining $NODE …"
kubectl drain "$NODE" \
  --ignore-daemonsets \
  --delete-emptydir-data \
  --force \
  --timeout=90s 2>&1 | grep -E '(evicting|error|warning)' || true

wait_until_ready "$INITIAL_READY"
S2_END=$(date +%s%N)

S2_MS=$(( (S2_END - S2_START) / 1000000 ))
S2_S=$(rto_ms_to_s "$S2_MS")
log "Node drain RTO : ${S2_S}s  (${S2_MS}ms)"

if (( S2_MS <= TARGET_NODE_RTO_S * 1000 )); then
  pass "Node RTO ${S2_S}s ≤ ${TARGET_NODE_RTO_S}s target"
else
  fail "Node RTO ${S2_S}s > ${TARGET_NODE_RTO_S}s target"
fi

log "Uncordoning $NODE …"
kubectl uncordon "$NODE"

# ─── Summary ──────────────────────────────────────────────────────────────────
sep
echo "  Results"
sep
printf "  %-28s %8s  (target ≤ %ds)\n" "Pod failure RTO:"  "${S1_S}s" "$TARGET_POD_RTO_S"
printf "  %-28s %8s  (target ≤ %ds)\n" "Node drain RTO:"   "${S2_S}s" "$TARGET_NODE_RTO_S"
sep
