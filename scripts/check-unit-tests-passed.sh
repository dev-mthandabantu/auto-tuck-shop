#!/usr/bin/env bash
#
# Refuse a production deploy unless unit_tests passed on the commit being released.
#
# Usage: check-unit-tests-passed.sh <repo> <sha>
#   repo  owner/name, e.g. aakitech/auto-tuck-shop
#   sha   the commit being released
#
# Exits 0 only when the most recently completed unit_tests check run for that
# commit concluded "success". Every other outcome exits non-zero, including
# ones where the answer could not be read at all: the deploy is refused rather
# than allowed on an unreadable response.
#
# Called by .github/workflows/deploy-production.yml. Tests: scripts/test-check-unit-tests-passed.sh

set -euo pipefail

repo="${1:?usage: check-unit-tests-passed.sh <repo> <sha>}"
sha="${2:?usage: check-unit-tests-passed.sh <repo> <sha>}"

if ! body=$(gh api "repos/$repo/commits/$sha/check-runs?check_name=unit_tests"); then
  echo "::error::Could not query check runs for $sha. Refusing to deploy."
  exit 1
fi

# No check_runs array means an error body or a changed API shape, not a passing
# build. Without this guard jq errors out and the conclusion is silently empty,
# which reports a test failure when the real problem is the response.
if ! echo "$body" | jq -e 'has("check_runs") and (.check_runs | type == "array")' >/dev/null 2>&1; then
  echo "::error::Unexpected response from the check-runs API. Refusing to deploy."
  echo "$body" | head -c 500
  exit 1
fi

# Re-runs mean a commit can have several runs. Take the newest completed one:
# an older success must not mask a newer failure.
conclusion=$(echo "$body" | jq -r '
  [.check_runs[] | select(.status == "completed")]
  | sort_by(.completed_at) | last | .conclusion // "missing"')

if [ "$conclusion" != "success" ]; then
  echo "::error::unit_tests is '$conclusion' for $sha. Refusing to deploy to production."
  echo "Re-run CI on this commit, or fix it and cut a new release."
  exit 1
fi

echo "unit_tests passed on $sha."
