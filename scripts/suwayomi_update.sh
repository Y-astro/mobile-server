#!/bin/bash
# Suwayomi Reading Category Update & Auto-Download Trigger
# Updates Category 1 ("Reading") and automatically queues new chapters for download.

if command -v python3 >/dev/null 2>&1 && [ -f "/data/data/com.termux/files/home/bin/suwayomi_daily_update.py" ]; then
    /data/data/com.termux/files/home/bin/suwayomi_daily_update.py --now
else
    echo "Triggering Suwayomi Reading category update..."
    curl -s -X POST http://127.0.0.1:4567/suwayomi/api/graphql \
      -H "Content-Type: application/json" \
      -d '{"query":"mutation { updateCategoryManga(input: { categories: [1] }) { clientMutationId } }"}' | grep -o '{"data":.*}' || echo "Update triggered."
fi
