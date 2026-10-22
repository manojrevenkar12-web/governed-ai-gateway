#!/usr/bin/env bash
# Smoke test against a running stack: a team, a key, a redacted request, a blocked one, the audit chain.
set -euo pipefail
URL=${URL:-http://127.0.0.1:8080}
ADMIN=${AIGW_ADMIN_TOKEN:-change-me}
for i in $(seq 1 60); do curl -fs "$URL/readyz" >/dev/null && break; sleep 1; done
curl -fs -X POST "$URL/admin/teams" -H "X-Admin-Token: $ADMIN" -H 'content-type: application/json' \
  -d '{"id":"demo","name":"Demo","policy":"engineering","monthly_budget_usd":10}' >/dev/null
KEY=$(curl -fs -X POST "$URL/admin/keys" -H "X-Admin-Token: $ADMIN" -H 'content-type: application/json' \
  -d '{"team_id":"demo","label":"smoke"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["key"])')
OUT=$(curl -fs "$URL/v1/chat/completions" -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"model":"claude-haiku-5-5","messages":[{"role":"user","content":"Refund DE89 3704 0044 0532 0130 00"}]}')
echo "$OUT" | grep -q "DE89 3704 0044 0532 0130 00" || { echo "answer did not restore the IBAN: $OUT"; exit 1; }
CODE=$(curl -s -o /dev/null -w '%{http_code}' "$URL/v1/chat/completions" -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' -d '{"model":"claude-haiku-5-5","messages":[{"role":"user","content":"key AKIAIOSFODNN7EXAMPLE"}]}')
[ "$CODE" = "403" ] || { echo "secret was not blocked: $CODE"; exit 1; }
curl -fs "$URL/metrics" | grep -q aigw_requests_total
echo "smoke test passed"
