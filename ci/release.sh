#!/usr/bin/env sh
# Build the wheel and sdist with the web UI included, into dist/.
set -eu
cd "$(dirname "$0")/.."
rm -rf dist src/brain/web/static
ci/frontend.sh
uv build
# Fail if the UI did not make it into the wheel.
python3 -c "import sys, zipfile, glob; w = glob.glob('dist/*.whl')[0]; sys.exit(0 if 'brain/web/static/index.html' in zipfile.ZipFile(w).namelist() else 'web UI missing from ' + w)"
