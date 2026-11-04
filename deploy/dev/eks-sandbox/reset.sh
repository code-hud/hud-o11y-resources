#!/usr/bin/env bash
# Undoes the EKS onboarding in the sandbox and keeps the cluster: removes the sample app and the collector release,
# deletes the onboarding stack, then the AMP workspace and pipeline log group it retains.
# Usage: deploy/dev/eks-sandbox/reset.sh   (env: STACK, default hud-eks-sandbox)
set -euo pipefail
export AWS_PROFILE=sandbox AWS_REGION=us-east-1
STACK=${STACK:-hud-eks-sandbox}
HERE=$(dirname "$0")

[[ $(aws sts get-caller-identity --query Account --output text) == 529601496549 ]] || { echo "not the sandbox account" >&2; exit 1; }
# A kubeconfig of its own, so your current kubectl context stays as it is.
export KUBECONFIG=$(mktemp)
trap 'rm -f "$KUBECONFIG"' EXIT
aws eks update-kubeconfig --name hud-eks-sandbox >/dev/null

kubectl delete -f "$HERE/sample-app.yaml" --ignore-not-found
helm uninstall hud -n hud --ignore-not-found
kubectl delete namespace hud --ignore-not-found

if aws cloudformation describe-stacks --stack-name "$STACK" >/dev/null 2>&1; then
  [[ $(aws cloudformation describe-stacks --stack-name "$STACK" \
    --query 'Stacks[0].Tags[?Key==`hud:managed-by`].Value' --output text) == cursor-agent ]] \
    || { echo "$STACK isn't tagged hud:managed-by=cursor-agent; not deleting it" >&2; exit 1; }
  id=$(aws cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].StackId' --output text)
  ws=$(aws cloudformation describe-stacks --stack-name "$STACK" \
    --query 'Stacks[0].Outputs[?OutputKey==`AmpWorkspaceId`].OutputValue' --output text)
  aws cloudformation delete-stack --stack-name "$STACK"
  aws cloudformation wait stack-delete-complete --stack-name "$id"
  [[ $ws && $ws != None ]] && aws amp delete-workspace --workspace-id "$ws"
  for g in $(aws logs describe-log-groups --log-group-name-prefix /aws/vendedlogs/OpenSearchIngestion/ \
    --query "logGroups[?ends_with(logGroupName, '/${id##*/}')].logGroupName" --output text); do
    aws logs delete-log-group --log-group-name "$g"
  done
fi
echo "reset done; cluster hud-eks-sandbox kept"
