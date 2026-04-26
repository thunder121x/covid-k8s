#!/usr/bin/env bash
# Guardian of the Cluster — one-shot local demo setup
# Prerequisites: minikube, kubectl, helm, docker, openssl, bc

set -euo pipefail

CLUSTER_NAME="guardian"
NODES=3
CPUS=4
MEMORY_MB=8192
K8S_DIR="$(cd "$(dirname "$0")/k8s" && pwd)"
HELM_DIR="$(cd "$(dirname "$0")/helm" && pwd)"
DOCKER_DIR="$(cd "$(dirname "$0")/docker" && pwd)"

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

step() { echo -e "\n${BOLD}${CYAN}▶ $1${NC}"; }
ok()   { echo -e "${GREEN}  ✓ $1${NC}"; }
info() { echo -e "${YELLOW}  • $1${NC}"; }

# ─── 0. Load .env then validate required secrets ──────────────────────────────
step "0 — Loading .env and validating secrets"
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$ROOT_DIR/.env" ]]; then
  # Export every non-comment, non-blank line from .env
  set -o allexport
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +o allexport
  info ".env loaded from $ROOT_DIR/.env"
else
  info "No .env file found — relying on shell environment"
fi

: "${DB_PASSWORD:?DB_PASSWORD is not set. Add it to .env or export it.}"
: "${DISCORD_ID:?DISCORD_ID is not set. Add it to .env or export it.}"
: "${DISCORD_TOKEN:?DISCORD_TOKEN is not set. Add it to .env or export it.}"

# Construct the full Discord webhook URL from split parts (matches namespace.yaml)
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/${DISCORD_ID}/${DISCORD_TOKEN}"
export DISCORD_WEBHOOK_URL
ok "Secrets loaded (DISCORD_WEBHOOK_URL constructed from DISCORD_ID + DISCORD_TOKEN)"

# ─── 1. Start minikube (idempotent) ──────────────────────────────────────────
step "1 — Starting minikube ($NODES nodes, ${CPUS} CPU, ${MEMORY_MB}MB RAM)"
if minikube status -p "$CLUSTER_NAME" 2>/dev/null | grep -q "Running"; then
  info "minikube already running — skipping"
else
  minikube start \
    -p "$CLUSTER_NAME" \
    --nodes "$NODES" \
    --cpus "$CPUS" \
    --memory "$MEMORY_MB" \
    --driver docker \
    --kubernetes-version stable \
    --addons ingress,metrics-server
  ok "minikube cluster started"
fi

# ─── 2. Enable addons ─────────────────────────────────────────────────────────
step "2 — Enabling addons"
minikube addons enable ingress        -p "$CLUSTER_NAME" || true
minikube addons enable metrics-server -p "$CLUSTER_NAME" || true
ok "ingress + metrics-server enabled"

# ─── 3. Build images on host Docker, then load into every minikube node ───────
# docker-env is incompatible with multi-node clusters; minikube image load
# distributes the image to all nodes so imagePullPolicy: Never keeps working.
step "3 — Building and loading Docker images"
docker build -t guardian/aqi-ingestor:latest    "$DOCKER_DIR/ingestor"
docker build -t guardian/dashboard-api:latest   "$DOCKER_DIR/dashboard-api"
docker build -t guardian/data-aggregator:latest "$DOCKER_DIR/aggregator"
ok "Images built"

info "Loading images into all $NODES minikube nodes (this takes ~1 min) …"
minikube image load guardian/aqi-ingestor:latest    -p "$CLUSTER_NAME"
minikube image load guardian/dashboard-api:latest   -p "$CLUSTER_NAME"
minikube image load guardian/data-aggregator:latest -p "$CLUSTER_NAME"
ok "Images loaded into cluster"

# ─── 4. Install kube-prometheus-stack ────────────────────────────────────────
step "4 — Installing kube-prometheus-stack"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
helm repo update

# Inject Discord webhook URL into values before installing
HELM_VALUES_TMP=$(mktemp /tmp/guardian-helm-values.XXXXXX.yaml)
sed "s|REPLACE_WITH_DISCORD_WEBHOOK_URL|${DISCORD_WEBHOOK_URL}|g" \
  "$HELM_DIR/values-prometheus.yaml" > "$HELM_VALUES_TMP"

helm upgrade --install kube-prometheus-stack \
  prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace \
  -f "$HELM_VALUES_TMP" \
  --wait --timeout 10m
rm -f "$HELM_VALUES_TMP"
ok "kube-prometheus-stack installed"

# ─── 5. Apply k8s manifests in dependency order ───────────────────────────────
step "5 — Applying Kubernetes manifests"

# 5a. Namespace + ConfigMap + Secret
# envsubst fills ${DB_PASSWORD}, ${DISCORD_ID}, ${DISCORD_TOKEN} in namespace.yaml
envsubst < "$K8S_DIR/namespace.yaml" | kubectl apply -f -
ok "Namespace, ConfigMap, Secret applied"

# 5b. Storage layer (must be ready before apps connect)
kubectl apply -f "$K8S_DIR/timescaledb-statefulset.yaml"
kubectl apply -f "$K8S_DIR/redis-deployment.yaml"
info "Waiting for TimescaleDB …"
kubectl rollout status statefulset/timescaledb -n guardian --timeout=120s
info "Waiting for Redis …"
kubectl rollout status deployment/redis -n guardian --timeout=60s
ok "Storage layer ready"

# 5c. Application deployments
kubectl apply -f "$K8S_DIR/ingestor-deployment.yaml"
kubectl apply -f "$K8S_DIR/dashboard-deployment.yaml"
kubectl apply -f "$K8S_DIR/aggregator-deployment.yaml"
ok "Application deployments applied"

# 5d. HPA (requires metrics-server)
kubectl apply -f "$K8S_DIR/hpa.yaml"
ok "HPA applied"

# 5e. Ingress
# Generate self-signed TLS cert for guardian.local
if ! kubectl get secret guardian-tls -n guardian &>/dev/null; then
  info "Generating self-signed TLS certificate …"
  openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
    -keyout /tmp/guardian.key \
    -out    /tmp/guardian.crt \
    -subj   "/CN=guardian.local/O=Guardian"
  kubectl create secret tls guardian-tls \
    -n guardian \
    --cert=/tmp/guardian.crt \
    --key=/tmp/guardian.key
  rm -f /tmp/guardian.key /tmp/guardian.crt
fi
kubectl apply -f "$K8S_DIR/ingress.yaml"
ok "Ingress applied"

# 5f. Prometheus alert rules (kube-prometheus-stack CRD must be installed first)
kubectl apply -f "$K8S_DIR/prometheus-rules.yaml"
ok "PrometheusRule applied"

# ─── 6. Wait for all pods to be ready ────────────────────────────────────────
step "6 — Waiting for all pods in guardian namespace"
kubectl rollout status deployment/aqi-ingestor   -n guardian --timeout=120s
kubectl rollout status deployment/dashboard-api  -n guardian --timeout=120s
kubectl rollout status deployment/data-aggregator -n guardian --timeout=120s
ok "All application pods ready"

# ─── 7. Print access information ─────────────────────────────────────────────
step "7 — Access information"
MINIKUBE_IP=$(minikube ip -p "$CLUSTER_NAME")

# minikube service --url blocks on Docker driver multi-node — print instructions instead
cat <<EOF

${BOLD}╔══════════════════════════════════════════════╗${NC}
${BOLD}║     Guardian of the Cluster — Ready          ║${NC}
${BOLD}╚══════════════════════════════════════════════╝${NC}

1. Add to /etc/hosts (run once):
   ${YELLOW}echo "${MINIKUBE_IP}  guardian.local" | sudo tee -a /etc/hosts${NC}

2. Start the ingress tunnel (keep this running in a separate terminal):
   ${YELLOW}minikube tunnel -p guardian${NC}

Endpoints (after tunnel is running):
   Dashboard API  : https://guardian.local/aqi
   Ingest (POST)  : https://guardian.local/ingest

3. Open Grafana (port-forward in a separate terminal):
   ${YELLOW}kubectl port-forward svc/kube-prometheus-stack-grafana 3000:80 -n monitoring${NC}
   Then open: http://localhost:3000
   User: admin  |  Password: guardian-admin

4. Open Prometheus:
   ${YELLOW}kubectl port-forward svc/kube-prometheus-stack-prometheus 9090:9090 -n monitoring${NC}
   Then open: http://localhost:9090

Chaos test:
   ${YELLOW}bash chaos/rto-test.sh${NC}

EOF

ok "Setup complete"
