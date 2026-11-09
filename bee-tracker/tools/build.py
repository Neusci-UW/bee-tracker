#!/usr/bin/env python3
"""Inline src/core.js into src/page.html and write bee-tracker.html (the single file people open)."""
from pathlib import Path

root = Path(__file__).resolve().parent.parent
page = (root / "src" / "page.html").read_text()
core = (root / "src" / "core.js").read_text()
assert page.count("/*@CORE@*/") == 1, "page.html must contain the /*@CORE@*/ placeholder once"
out = root / "bee-tracker.html"
out.write_text(page.replace("/*@CORE@*/", core.rstrip() + "\n"))
print(f"wrote {out.relative_to(root)} ({out.stat().st_size // 1024} KB)")
