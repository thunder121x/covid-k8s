#!/usr/bin/env bash
# Tears down the entire Guardian of the Cluster stack.
# Pass --hard to also delete the minikube cluster (default: stop only).

set -euo pipefail

CLUSTER_NAME="guardian"
HARD=false
[[ "${1:-}" == "--hard" ]] && HARD=true

BOLD=$'\033[1m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[1;33m'
RED=$'\033[0;31m'
CYAN=$'\033[0;36m'
NC=$'\033[0m'

step() { echo -e "\n${BOLD}${CYAN}▶ $1${NC}"; }
ok()   { echo -e "${GREEN}  ✓ $1${NC}"; }
info() { echo -e "${YELLOW}  • $1${NC}"; }
warn() { echo -e "${RED}  ! $1${NC}"; }

# ─── 1. Kill any background port-forwards ────────────────────────────────────
step "1 — Killing port-forwards"
pkill -f "kubectl port-forward.*guardian\|kubectl port-forward.*monitoring" 2>/dev/null && ok "Port-forwards killed" || info "No port-forwards found"

# ─── 2. Uninstall Helm releases ───────────────────────────────────────────────
step "2 — Uninstalling Helm releases"
if helm status kube-prometheus-stack -n monitoring &>/dev/null; then
  helm uninstall kube-prometheus-stack -n monitoring
  ok "kube-prometheus-stack uninstalled"
else
  info "kube-prometheus-stack not found — skipping"
fi

# ─── 3. Delete guardian namespace (removes all app resources + PVCs) ─────────
step "3 — Deleting guardian namespace"
if kubectl get namespace guardian &>/dev/null; then
  kubectl delete namespace guardian --timeout=60s
  ok "guardian namespace deleted"
else
  info "guardian namespace not found — skipping"
fi

# ─── 4. Delete monitoring namespace ──────────────────────────────────────────
step "4 — Deleting monitoring namespace"
if kubectl get namespace monitoring &>/dev/null; then
  kubectl delete namespace monitoring --timeout=60s
  ok "monitoring namespace deleted"
else
  info "monitoring namespace not found — skipping"
fi

# ─── 5. Stop or delete minikube cluster ──────────────────────────────────────
if $HARD; then
  step "5 — Deleting minikube cluster '$CLUSTER_NAME' (--hard)"
  warn "This removes the cluster, all PersistentVolumes, and cached images."
  minikube delete -p "$CLUSTER_NAME"
  ok "Cluster deleted"
else
  step "5 — Stopping minikube cluster '$CLUSTER_NAME'"
  info "Cluster state is preserved. Run 'bash setup.sh' to bring it back up."
  info "To fully delete: bash teardown.sh --hard"
  minikube stop -p "$CLUSTER_NAME"
  ok "Cluster stopped"
fi

echo -e "\n${BOLD}${GREEN}Teardown complete.${NC}\n"
