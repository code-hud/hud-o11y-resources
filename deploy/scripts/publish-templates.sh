#!/usr/bin/env bash
# Publishes every template in deploy/aws/ for one release: to templates/<version>/ in the bucket, then to the flat
# templates/ prefix that the quick-create links use (skip that with LATEST=0). In every copy the TemplatesUrl
# default points at templates/<version>, so a parent always nests the children published with it.
# The version is the release tag: the argument, or the tag on HEAD. A published version isn't overwritten (FORCE=1 does).
# Usage: deploy/scripts/publish-templates.sh [--dryrun] [version]   (env: BUCKET, OUT, LATEST, FORCE)
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

if [[ ! $DRY && ${FORCE:-} != 1 ]] && aws s3api head-object --bucket "$BUCKET" --key "templates/$VERSION/hud.yaml" >/dev/null 2>&1; then
  echo "templates/$VERSION/ is already published (FORCE=1 overwrites it)" >&2; exit 1
fi
# The version folder goes first, so the flat prefix's parents never point at children that aren't there yet.
run aws s3 cp --recursive "$OUT/$VERSION/" "s3://$BUCKET/templates/$VERSION/"
if [[ ${LATEST:-1} == 1 ]]; then
  run aws s3 cp --recursive "$OUT/$VERSION/" "s3://$BUCKET/templates/"
  [[ $DRY ]] && cp "$OUT/$VERSION"/*.yaml "$OUT/"
fi
echo "quick-create base URL: $URL"
