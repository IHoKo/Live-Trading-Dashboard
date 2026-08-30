"""Test fixtures.

`app.main` resolves the static directory at import time, so STATIC_DIR has to be
set before the app module is imported anywhere — hence the module-level setup
here rather than a fixture.
"""

import os
import tempfile
from pathlib import Path

_static = Path(tempfile.mkdtemp(prefix="ticker-static-"))
(_static / "assets").mkdir()
(_static / "index.html").write_text("<!doctype html><title>Ticker</title>", encoding="utf-8")
(_static / "assets" / "index-abc123.js").write_text("console.log(0)", encoding="utf-8")
(_static / "favicon.ico").write_bytes(b"\x00")

os.environ["STATIC_DIR"] = str(_static)

STATIC_DIR = _static
