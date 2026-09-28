#!/bin/bash
# test-api.sh — exercise the running Stage 1 service
#
# Usage:  bash test-api.sh
# Requires only curl + python3. Does not touch ~/Downloads or the repo.

API=${API:-http://localhost:8000}

# Buffer stdin so the raw text is still available when it isn't pure JSON
# (POST calls append an "HTTP <code>" line, which json.tool rejects).
pp() { local s; s=$(cat); python3 -m json.tool <<<"$s" 2>/dev/null || printf '%s\n' "$s"; }
hr() { echo ""; echo "── $1 ─────────────────────────────────────────"; }

if ! curl -sf "$API/health" >/dev/null 2>&1; then
    echo "✗ No service at $API"
    echo "  Start it with: docker run --rm -p 8000:8000 pocketful:stage1"
    exit 1
fi

hr "health"
curl -s "$API/health" | pp

hr "alice before"
curl -s "$API/account/alice" | pp

hr "transfer \$50 alice -> bob  (expect 200 / success)"
curl -s -w "\nHTTP %{http_code}\n" -X POST "$API/transfer" \
    -H "Content-Type: application/json" \
    -d '{"sender_id":"alice","receiver_id":"bob","amount_dollars":50.0,"txn_id":"txn_001"}' | pp

hr "replay same txn_id  (expect 200 / status=duplicate)"
curl -s -w "\nHTTP %{http_code}\n" -X POST "$API/transfer" \
    -H "Content-Type: application/json" \
    -d '{"sender_id":"alice","receiver_id":"bob","amount_dollars":50.0,"txn_id":"txn_001"}' | pp

hr "overdraft  (expect 409 / LEDGER_INSUFFICIENT_BALANCE)"
curl -s -w "\nHTTP %{http_code}\n" -X POST "$API/transfer" \
    -H "Content-Type: application/json" \
    -d '{"sender_id":"alice","receiver_id":"bob","amount_dollars":999999.0,"txn_id":"txn_002"}' | pp

hr "unknown account  (expect 404 / LEDGER_INVALID_ACCOUNT)"
curl -s -w "\nHTTP %{http_code}\n" -X POST "$API/transfer" \
    -H "Content-Type: application/json" \
    -d '{"sender_id":"nobody","receiver_id":"bob","amount_dollars":10.0,"txn_id":"txn_003"}' | pp

hr "fetch txn_001"
curl -s "$API/transaction/txn_001" | pp

hr "verify  (expect is_balanced=true, num_transactions=1)"
curl -s "$API/verify" | pp

hr "ledger overview"
curl -s "$API/ledger" | pp

echo ""
echo "════════════════════════════════════════════════"
echo "Check these three:"
echo "  • replay returned status=duplicate, not success"
echo "  • alice = 4950.00 (one \$50 debit, not two)"
echo "  • num_transactions = 1"
echo "════════════════════════════════════════════════"
