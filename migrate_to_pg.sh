#!/bin/bash
# Universal Migration Script Runner
# Runs migrate_to_pg.py as postgres user

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "🚀 Starting universal migration..."
echo "📁 Working directory: $SCRIPT_DIR"

# Run migration as postgres user
su - postgres -c "cd '$SCRIPT_DIR' && python3 migrate_to_pg.py"

echo "✅ Migration script finished."
