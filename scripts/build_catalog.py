"""Generate src/story_service/assets/catalog.json from the asset files (Section 6.7). Run after changing any asset.

python scripts/build_catalog.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from story_service.assets.catalog import CATALOG_FILE, build_catalog  # noqa: E402

if __name__ == "__main__":
    cat = build_catalog()
    CATALOG_FILE.write_text(json.dumps(cat, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        f"{CATALOG_FILE.name}: {len(cat['backgrounds'])} backgrounds, {len(cat['props'])} props, "
        f"{len(cat['widgets'])} widgets, {len(cat['poses'])} poses, {len(cat['expressions'])} expressions"
    )
