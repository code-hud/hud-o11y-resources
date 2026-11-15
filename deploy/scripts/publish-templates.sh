#!/usr/bin/env bash
# Publishes every template in deploy/aws/ for one release: to templates/<version>/ in the bucket, then, only when the
# version is the highest X.Y.Z tag in the repository, to the flat templates/ prefix that the quick-create links use.
# In every copy the TemplatesUrl default points at templates/<version>, so a parent always nests the children
# published with it. The version is the release tag: the argument, or the tag on HEAD. A published version is never
# overwritten, and the run aborts if S3 can't say for sure that it isn't published.
# Usage: deploy/scripts/publish-templates.sh [--dryrun] [version]   (env: BUCKET, OUT)
# --dryrun renders into OUT (a temp dir by default, laid out like the bucket's templates/) and prints the uploads.
set -euo pipefail
cd "$(dirname "$0")/../.."

DRY=
[[ ${1:-} == --dryrun ]] && { DRY=1; shift; }
VERSION=${1:-$(git describe --tags --exact-match 2>/dev/null)} || { echo "no version: pass one or tag HEAD" >&2; exit 1; }
[[ $VERSION =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ && $VERSION != latest ]] || { echo "invalid version: $VERSION" >&2; exit 1; }
BUCKET=${BUCKET:-hud-cloudformation-templates}
OUT=${OUT:-$(mktemp -d)}
URL=https://$BUCKET.s3.amazonaws.com/templates/$VERSION
DEFAULT='    Default: https://hud-cloudformation-templates.s3.amazonaws.com/templates'

run() { if [[ $DRY ]]; then echo "+ $*"; else "$@"; fi; }

python3 deploy/scripts/unify.py --check >/dev/null

mkdir -p "$OUT/$VERSION"
for f in deploy/aws/*.yaml; do
  t=$(<"$f")
  printf '%s\n' "${t//$'\n'$DEFAULT$'\n'/$'\n'    Default: $URL$'\n'}" >"$OUT/$VERSION/${f##*/}"
done
if grep -l "^$DEFAULT\$" "$OUT/$VERSION"/*.yaml; then echo "TemplatesUrl default not rewritten in the files above" >&2; exit 1; fi
echo "rendered $(ls "$OUT/$VERSION" | wc -l | tr -d ' ') templates into $OUT/$VERSION (TemplatesUrl: $URL)"

if [[ ! $DRY ]]; then
  if err=$(aws s3api head-object --bucket "$BUCKET" --key "templates/$VERSION/hud.yaml" 2>&1 >/dev/null); then
    echo "templates/$VERSION/ is already published" >&2; exit 1
  elif [[ $err != *"(404)"* && $err != *NoSuchKey* ]]; then
    echo "can't tell whether templates/$VERSION/ is published: $err" >&2; exit 1
  fi
fi
HIGHEST=$(git tag -l | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -t. -k1,1n -k2,2n -k3,3n | tail -n1 || true)
# The version folder goes first, so the flat prefix's parents never point at children that aren't there yet.
run aws s3 cp --recursive "$OUT/$VERSION/" "s3://$BUCKET/templates/$VERSION/"
if [[ $VERSION == "$HIGHEST" ]]; then
  run aws s3 cp --recursive "$OUT/$VERSION/" "s3://$BUCKET/templates/"
  [[ $DRY ]] && cp "$OUT/$VERSION"/*.yaml "$OUT/"
else
  echo "not updating templates/: $VERSION isn't the highest X.Y.Z tag (${HIGHEST:-none})"
fi
echo "quick-create base URL: $URL"
