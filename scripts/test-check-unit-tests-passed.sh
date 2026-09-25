#!/usr/bin/env bash
#
# Tests for check-unit-tests-passed.sh.
#
# Runs the real script against a fake `gh` placed first on PATH, so each case
# drives the script with a canned API response. No network, no credentials.
#
# Usage: scripts/test-check-unit-tests-passed.sh
# Exits 0 if every case behaves as expected, 1 otherwise.

set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script="$here/check-unit-tests-passed.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

pass=0
fail=0

# run_case <description> <expected: deploy|refuse> <gh-exit-code> <gh-stdout>
run_case() {
  local desc="$1" expected="$2" gh_exit="$3" payload="$4"

  mkdir -p "$tmp/bin"
  cat > "$tmp/bin/gh" <<FAKE
#!/usr/bin/env bash
[ $gh_exit -ne 0 ] && exit $gh_exit
cat <<'PAYLOAD'
$payload
PAYLOAD
FAKE
  chmod +x "$tmp/bin/gh"

  PATH="$tmp/bin:$PATH" bash "$script" aakitech/auto-tuck-shop deadbeef >/dev/null 2>&1
  local rc=$?
  local actual="refuse"
  [ "$rc" -eq 0 ] && actual="deploy"

  if [ "$actual" = "$expected" ]; then
    pass=$((pass + 1))
    printf '  ok   %-42s %s\n' "$desc" "$actual"
  else
    fail=$((fail + 1))
    printf '  FAIL %-42s expected %s, got %s (exit %d)\n' "$desc" "$expected" "$actual" "$rc"
  fi
}

ts() { printf '2026-09-%02dT10:00:00Z' "$1"; }

echo "check-unit-tests-passed.sh"

# --- a passing build is the only case that deploys ---
run_case "single success" deploy 0 \
  "{\"check_runs\":[{\"status\":\"completed\",\"completed_at\":\"$(ts 1)\",\"conclusion\":\"success\"}]}"

run_case "single failure" refuse 0 \
  "{\"check_runs\":[{\"status\":\"completed\",\"completed_at\":\"$(ts 1)\",\"conclusion\":\"failure\"}]}"

# --- re-runs: the newest completed run decides, not any success in the list ---
run_case "re-run: old failure, new success" deploy 0 \
  "{\"check_runs\":[{\"status\":\"completed\",\"completed_at\":\"$(ts 1)\",\"conclusion\":\"failure\"},{\"status\":\"completed\",\"completed_at\":\"$(ts 2)\",\"conclusion\":\"success\"}]}"

# The case a naive "is there a success" check would get wrong.
run_case "re-run: old success, newer failure" refuse 0 \
  "{\"check_runs\":[{\"status\":\"completed\",\"completed_at\":\"$(ts 2)\",\"conclusion\":\"failure\"},{\"status\":\"completed\",\"completed_at\":\"$(ts 1)\",\"conclusion\":\"success\"}]}"

# --- no usable conclusion ---
run_case "empty check_runs array" refuse 0 '{"check_runs":[]}'

run_case "in-progress only" refuse 0 \
  '{"check_runs":[{"status":"in_progress","completed_at":null,"conclusion":null}]}'

run_case "cancelled" refuse 0 \
  "{\"check_runs\":[{\"status\":\"completed\",\"completed_at\":\"$(ts 1)\",\"conclusion\":\"cancelled\"}]}"

run_case "skipped" refuse 0 \
  "{\"check_runs\":[{\"status\":\"completed\",\"completed_at\":\"$(ts 1)\",\"conclusion\":\"skipped\"}]}"

# --- the response itself is not usable: refuse, never assume a pass ---
run_case "404 error body" refuse 0 '{"message":"Not Found","status":"404"}'
run_case "wrong shape, field renamed" refuse 0 '{"checkRuns":[{"status":"completed","conclusion":"success"}]}'
run_case "empty body" refuse 0 ''
run_case "HTML body" refuse 0 '<html>502 Bad Gateway</html>'
run_case "check_runs not an array" refuse 0 '{"check_runs":{"status":"completed"}}'
run_case "gh api exits non-zero" refuse 1 ''

# --- the shape guard must explain itself, not just exit non-zero ---
# Without the guard, set -e still makes jq's error exit 1, so an exit-code-only
# assertion passes with the guard deleted. What the guard actually buys is a
# readable reason: check the message, or this tests nothing.
mkdir -p "$tmp/bin"
cat > "$tmp/bin/gh" <<'FAKE'
#!/usr/bin/env bash
echo '{"checkRuns":[{"status":"completed","conclusion":"success"}]}'
FAKE
chmod +x "$tmp/bin/gh"
out=$(PATH="$tmp/bin:$PATH" bash "$script" aakitech/auto-tuck-shop deadbeef 2>&1)
if echo "$out" | grep -q "Unexpected response from the check-runs API"; then
  pass=$((pass + 1))
  printf '  ok   %-42s %s\n' "wrong shape names the response" "refuse"
else
  fail=$((fail + 1))
  printf '  FAIL %-42s no shape-guard message in output\n' "wrong shape names the response"
fi

# A jq parse error must never be what the operator sees.
if echo "$out" | grep -qi "^jq: error"; then
  fail=$((fail + 1))
  printf '  FAIL %-42s raw jq error leaked to output\n' "no raw jq error"
else
  pass=$((pass + 1))
  printf '  ok   %-42s %s\n' "no raw jq error" "clean"
fi

# --- called wrong: the workflow passes both args, but a future edit might not ---
# These bypass run_case because they deliberately call the script with no payload.
for args in "" "aakitech/auto-tuck-shop"; do
  # shellcheck disable=SC2086
  bash "$script" $args >/dev/null 2>&1
  rc=$?
  label="missing args: '${args:-none}'"
  if [ "$rc" -ne 0 ]; then
    pass=$((pass + 1))
    printf '  ok   %-42s refuse\n' "$label"
  else
    fail=$((fail + 1))
    printf '  FAIL %-42s expected refuse, got exit 0\n' "$label"
  fi
done

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
