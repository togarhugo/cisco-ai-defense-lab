#!/usr/bin/env bash
# Checks the Cisco AI Defense Inspection API from INSIDE the cluster, with the same key, region and network as LabBot:
# the chat endpoint (proves key + region) and several MCP message shapes (what the agent uses). Harmless messages only.
#   ./scripts/test-mcp-inspect.sh
set -euo pipefail
NS=ai-defense-lab
KUBECTL="${KUBECTL:-kubectl}"
$KUBECTL -n "$NS" exec -i deploy/labbot -- python - < "$(dirname "$0")/diagnose_inspection.py"
