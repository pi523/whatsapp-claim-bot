#!/bin/bash
# Start the expense-claim service as a standalone pm2 process (claim-service).
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# Free port 5005 (kill any stale process)
lsof -ti:5005 | xargs kill -9 2>/dev/null || true
sleep 1

# Keep it alive with pm2; process name: claim-service
pm2 delete claim-service 2>/dev/null || true
pm2 start claim_service.py --name claim-service --interpreter python3

echo "✅ claim-service started (http://127.0.0.1:5005/health)"
echo "Logs: pm2 logs claim-service"
