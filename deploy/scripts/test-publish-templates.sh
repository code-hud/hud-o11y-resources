#!/usr/bin/env bash
# Checks that publish-templates.sh --dryrun updates the flat templates/ prefix only for the highest X.Y.Z tag.
# Runs it in a throwaway git repo holding a copy of deploy/. Needs PyYAML.
set -euo pipefail
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cp -R "$(dirname "$0")/.." "$tmp/deploy"
cd "$tmp"
git init -q && git add . && git -c user.name=t -c user.email=t@t commit -qm t
for t in 1.2.3 1.9.0 1.10.0 2.0.0-rc1 9.9; do git tag "$t"; done

flat() { OUT="$tmp/out-$1" deploy/scripts/publish-templates.sh --dryrun "$1" | grep -c ' s3://hud-cloudformation-templates/templates/$' || true; }
[[ $(flat 1.10.0) == 1 ]] || { echo "FAIL: 1.10.0 is the highest tag but templates/ isn't updated" >&2; exit 1; }
for v in 1.9.0 1.2.3 2.0.0-rc1 test-1; do
  [[ $(flat "$v") == 0 ]] || { echo "FAIL: $v updates templates/" >&2; exit 1; }
done
echo ok
