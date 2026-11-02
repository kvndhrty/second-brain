#!/usr/bin/env sh
# Typecheck, test, and build the web UI. Needs Node 22+ (see frontend/.node-version).
set -eu
cd "$(dirname "$0")/../frontend"
npm ci --no-audit --no-fund
npm run typecheck
npm test
npm run build
