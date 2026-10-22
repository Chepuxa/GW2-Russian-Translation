"""Local Russian adapter for NikeGipple's Local Translator v0.10.0.

Uses the upstream protocol and engine with Unicode-safe tokenizer loading. No hosted
translation service or automatic Russian patch updates. Community discovery has
its own opt-in local viewer, separate from the upstream correction editor.
"""
from pathlib import Path
import sys

if not getattr(sys, "frozen", False):
    sources = Path(__file__).resolve().parent / "source/src"
    if sources.is_dir():
        sys.path.insert(0, str(sources))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import lt_server as lt
import lt_module


lt.LANGUAGES["ru"] = lt.Language(
    "ru", "ru", "RU", "local-only",
    {"Recipe": "Рецепт: ", "Recipes": "Рецепты: ",
     "Recipe[s]": 'Рецепт[pl:"Рецепты"]: '},
    r"(?!)", plural=False,
)
lt_module.VERSION = "0.10.0-ru-local6"


def require_local_model(model_dir: Path, data_dir: Path, url: str) -> None:
    required = ("model.bin", "config.json", "shared_vocabulary.json",
                "source.spm", "target.spm")
    missing = [name for name in required if not (model_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(f"Russian model is incomplete in {model_dir}: {missing}")


lt.ensure_model = require_local_model

if __name__ == "__main__":
    if "--contribute" in sys.argv:
        import contribution_ui
        raise SystemExit(contribution_ui.main([arg for arg in sys.argv[1:] if arg != "--contribute"]))
    if "--install-reviewed" in sys.argv:
        import reviewed_setup
        raise SystemExit(reviewed_setup.main([arg for arg in sys.argv[1:] if arg != "--install-reviewed"]))
    if "--precache" in sys.argv:
        import precache_tool
        try:
            raise SystemExit(precache_tool.main([arg for arg in sys.argv[1:] if arg != "--precache"]))
        except Exception:
            # The progress file contains the actionable error; no GUI error dialog in a piped module.
            raise SystemExit(1)
    raise SystemExit(lt_module.main())
