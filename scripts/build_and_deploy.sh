#!/usr/bin/env bash
# Build Docker images inside minikube's daemon and apply all k8s manifests.
# Run from the repo root: bash scripts/build_and_deploy.sh
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

echo "==> Pointing Docker at minikube's daemon..."
eval "$(minikube docker-env)"

echo "==> Building api-service..."
docker build -t api-service:latest services/api/

echo "==> Building geo-service..."
docker build -t geo-service:latest services/geo/

echo "==> Applying Kubernetes manifests..."
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/localstack.yaml
kubectl apply -f k8s/geo.yaml
kubectl apply -f k8s/api.yaml

echo "==> Waiting for pods to be ready..."
kubectl rollout status deployment/localstack  --timeout=120s
kubectl rollout status deployment/geo-service --timeout=120s
kubectl rollout status deployment/api-service --timeout=120s

MINIKUBE_IP=$(minikube ip)
echo ""
echo "==> Deployment complete!"
echo "    API:        http://${MINIKUBE_IP}:30000"
echo "    LocalStack: http://${MINIKUBE_IP}:31566"
echo ""
echo "==> To set up AWS resources and ingest tiles:"
echo "    kubectl port-forward service/localstack 4566:4566 &"
echo "    python scripts/setup_aws.py"
echo "    python scripts/ingest_tiles.py"
