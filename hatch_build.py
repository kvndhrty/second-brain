"""Build hook: make sure the wheel contains the built web UI.

The UI is built from frontend/ into src/brain/web/static/ (which is not in
git). If it is already there (a release build, or an sdist that includes it)
nothing happens. Otherwise this runs `npm ci` and `npm run build`, so Node is
needed only when building from a git checkout. Set BRAIN_SKIP_FRONTEND=1 to
build without the UI; `brain start` then serves the API and MCP only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class FrontendBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        if version == "editable":
            return  # development installs use `npm run dev` or a manual build
        root = Path(self.root)
        static = root / "src" / "brain" / "web" / "static"
        frontend = root / "frontend"
        if (static / "index.html").exists() or not (frontend / "package.json").exists():
            return
        if os.environ.get("BRAIN_SKIP_FRONTEND") == "1":
            self.app.display_warning("BRAIN_SKIP_FRONTEND=1: building without the web UI")
            return
        npm = shutil.which("npm")
        if npm is None:
            raise RuntimeError(
                "The web UI is not built and npm is not on PATH. Install Node 22 or newer (npm comes with it), "
                "or install a release wheel that already contains the UI, or set BRAIN_SKIP_FRONTEND=1 to "
                "build without it."
            )
        self.app.display_info("building the web UI (npm ci && npm run build)")
        for args in ([npm, "ci", "--no-audit", "--no-fund"], [npm, "run", "build"]):
            subprocess.run(args, cwd=frontend, check=True)
