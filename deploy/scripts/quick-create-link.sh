#!/usr/bin/env bash
# Prints a new ExternalId and the "Launch in CloudFormation" link of each one-stack template with it prefilled, the
# way Hud's onboarding builds them: hud-ecs.yaml for services on ECS, hud.yaml for EKS or anywhere else (add
# &param_CollectorPlatform=None when the customer runs the collector themselves). Store the ExternalId with the customer: Hud sends it when assuming their
# role.
# Usage: deploy/scripts/quick-create-link.sh [external-id]   (env: REGION, BASE_URL, STACK_NAME)
set -euo pipefail

REGION=${REGION:-us-east-1}
BASE_URL=${BASE_URL:-https://hud-cloudformation-templates.s3.amazonaws.com/templates}
STACK_NAME=${STACK_NAME:-hud}
ID=${1:-$( (uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid) | tr 'A-Z' 'a-z')}
# hud-access.yaml's ExternalId constraints
# (BSD regex caps repeat counts at 255, so the length is checked separately.)
[[ $ID =~ ^[A-Za-z0-9_+=,.@:/-]+$ ]] && (( ${#ID} >= 2 && ${#ID} <= 1224 )) || { echo "invalid ExternalId: $ID" >&2; exit 1; }

enc() { local s=${1//%/%25}; s=${s//:/%3A}; s=${s//\//%2F}; s=${s//+/%2B}; s=${s//=/%3D}; s=${s//,/%2C}; s=${s//@/%40}; echo "$s"; }

echo "ExternalId: $ID"
for t in hud-ecs.yaml hud.yaml; do
  echo "$t: https://$REGION.console.aws.amazon.com/cloudformation/home?region=$REGION#/stacks/quickcreate?templateURL=$(enc "$BASE_URL/$t")&stackName=$STACK_NAME&param_ExternalId=$(enc "$ID")"
done
