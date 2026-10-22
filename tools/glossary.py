"""Validate the shared editorial glossary; it is not a machine replacement dictionary."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import catalog

ROOT = Path(__file__).resolve().parents[1]
PATH = "glossary/ru.json"
DEFAULT = {"format": 1, "language": "ru",
           "note": "Редакторские договорённости. Не используется для слепой замены слов во всех строках игры.",
           "terms": []}


def identity(term):
    pair = [term["en"].strip().casefold(), term["context"].strip().casefold()]
    return hashlib.sha256(json.dumps(pair, ensure_ascii=False).encode("utf-8")).hexdigest()


def validate(document):
    if (not isinstance(document, dict) or type(document.get("format")) is not int or
            document["format"] != 1 or document.get("language") != "ru" or
            not isinstance(document.get("terms"), list)):
        raise ValueError("Словарь: требуется format=1, language=ru и список terms")
    if "note" in document and not isinstance(document["note"], str):
        raise ValueError("Словарь: note должен быть текстом")
    seen = set()
    for term in document["terms"]:
        if not isinstance(term, dict) or set(term) != {"en", "ru", "context"}:
            raise ValueError("Термин должен содержать en, ru и context")
        for field, label in (("en", "Английский термин"), ("ru", "Русский вариант"), ("context", "Контекст")):
            value = term[field]
            if not isinstance(value, str) or (field != "context" and not value.strip()):
                raise ValueError(label + ": требуется текст")
            if len(value.encode("utf-8")) > (8000 if field == "context" else 2000) or catalog.CONTROLS.search(value):
                raise ValueError(label + ": недопустимая длина или управляющие символы")
        key = identity(term)
        if key in seen:
            raise ValueError("Такой английский термин с этим контекстом уже есть в словаре")
        seen.add(key)
    return document


def load(root=ROOT):
    document = json.loads((root / PATH).read_text(encoding="utf-8-sig"), object_pairs_hook=catalog.unique_object)
    return validate(document)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check",))
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        print(json.dumps({"valid": True, "terms": len(load(args.root)["terms"])}))
        return 0
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
