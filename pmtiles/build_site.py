#!/usr/bin/env python3
"""
Q12 - Assemble the static GitHub Pages site
===========================================

Copies the same MapLibre client used with the live Docker stack plus the
PMTiles archive into one folder. On GitHub Pages /api/config does not exist,
so client/app.js switches itself to static PMTiles mode.

    _site/index.html, app.js, style.css      <- client/
    _site/tiles/kochi.pmtiles                <- docs/tiles/
    _site/tiles/kochi_static.json            <- docs/tiles/
    _site/.nojekyll                          (serve files as-is)

Used by .github/workflows/deploy-pmtiles.yml and for local testing:
    python pmtiles\\build_site.py
    python pmtiles\\serve_static.py      # http://localhost:8090
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIENT_FILES = ["index.html", "app.js", "style.css"]
TILE_FILES = ["kochi.pmtiles", "kochi_static.json"]


def main() -> int:
    p = argparse.ArgumentParser(description="Assemble the static PMTiles site")
    p.add_argument("--out", type=Path, default=ROOT / "_site")
    args = p.parse_args()
    out: Path = args.out

    missing = [f"client/{f}" for f in CLIENT_FILES if not (ROOT / "client" / f).exists()]
    missing += [f"docs/tiles/{f}" for f in TILE_FILES if not (ROOT / "docs" / "tiles" / f).exists()]
    if missing:
        print("Missing files: " + ", ".join(missing))
        print("Run 'python pmtiles/export_pmtiles.py' with the Docker stack up, then commit docs/tiles/.")
        return 1

    if out.exists():
        shutil.rmtree(out)
    (out / "tiles").mkdir(parents=True)
    for f in CLIENT_FILES:
        shutil.copy2(ROOT / "client" / f, out / f)
    for f in TILE_FILES:
        shutil.copy2(ROOT / "docs" / "tiles" / f, out / "tiles" / f)
    (out / ".nojekyll").write_text("", encoding="utf-8")

    meta = json.loads((out / "tiles" / "kochi_static.json").read_text(encoding="utf-8"))
    total = 0
    for path in sorted(out.rglob("*")):
        if path.is_file():
            size = path.stat().st_size
            total += size
            print(f"  {path.relative_to(out).as_posix():<28} {size:>10,} bytes")
    print(f"Site ready in {out} ({total / 1e6:.2f} MB); PMTiles from {meta['generated_at_utc']}, "
          f"{meta['tiles_written']} tiles, layers {list(meta['layers'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
