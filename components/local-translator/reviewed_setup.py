"""Install the bundled contextual catalog without requiring a separate Python."""
from pathlib import Path
import argparse
import json
import os

import catalog


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-dir", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        rows = catalog.load_catalog(args.catalog)
        result = catalog.install(rows, args.game_dir, module_stopped=False,
                                 initialize_cache=True)
        result["phase"] = "complete"
        code = 0
    except Exception as exc:
        result = {"phase": "failed", "error": str(exc)}
        code = 1
    args.report.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.report.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, args.report)
    return code
