#!/usr/bin/env bash
# Fetch an evidence release into the gitignored releases/ cache.
#
#   ./scripts/fetch-release.sh run-2-rome            # saves, screenshots, notes (small)
#   ./scripts/fetch-release.sh run-2-rome --video    # the above plus recordings (large)
#
# The cache is disposable: GitHub is the source of truth. Delete releases/ any time.
set -euo pipefail

TAG="${1:?usage: fetch-release.sh <tag> [--video]}"
REPO="diegoami/imp_conquest_original"
DIR="$(cd "$(dirname "$0")/.." && pwd)/releases/$TAG"
mkdir -p "$DIR"

PATTERNS=(--pattern '*.sav' --pattern '*.png' --pattern '*.txt')
if [ "${2:-}" = "--video" ]; then
  PATTERNS+=(--pattern '*.mp4')
  echo "Fetching $TAG WITH recordings (this may be hundreds of MB)."
else
  echo "Fetching $TAG without recordings. Pass --video to include them."
fi

gh release download "$TAG" --repo "$REPO" --dir "$DIR" --clobber "${PATTERNS[@]}"
echo
echo "Fetched into $DIR:"
find "$DIR" -type f -printf '  %f\n' | sort
