#!/usr/bin/env python3
"""Extract UI messages, update catalogs, and regenerate complete English."""

from __future__ import annotations

import argparse
import re
import subprocess
import tempfile
from pathlib import Path

DOMAIN = "io.github.leesethefox.Clipper"
REPO_ROOT = Path(__file__).resolve().parent.parent
PO_DIR = REPO_ROOT / "po"
POT_FILE = PO_DIR / "clipper.pot"


def _python_sources() -> list[str]:
    return [
        str(path.relative_to(REPO_ROOT))
        for path in sorted((REPO_ROOT / "ui").glob("*.py"))
        if not path.name.startswith("test_")
    ]


def _run(*arguments: str) -> None:
    subprocess.run(arguments, cwd=REPO_ROOT, check=True)


def _set_english_headers(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    text = text.replace("#, fuzzy\nmsgid \"\"", 'msgid ""', 1)
    text = text.replace('"Language: \\n"', '"Language: en\\n"')
    text = text.replace(
        '"Plural-Forms: nplurals=INTEGER; plural=EXPRESSION;\\n"',
        '"Plural-Forms: nplurals=2; plural=(n != 1);\\n"',
    )
    marker = '"Content-Transfer-Encoding: 8bit\\n"\n'
    additions = (
        '"X-Clipper-Language-Name: English\\n"\n'
        '"X-Clipper-Text-Direction: ltr\\n"\n'
    )
    if "X-Clipper-Language-Name:" not in text:
        text = text.replace(marker, marker + additions)
    path.write_text(text, encoding="utf-8")


def _normalize_timestamps(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    text = re.sub(
        r'"POT-Creation-Date: [^\\n]*\\n"',
        lambda _match: '"POT-Creation-Date: YEAR-MO-DA HO:MI+ZONE\\n"',
        text,
    )
    text = re.sub(
        r'"PO-Revision-Date: [^\\n]*\\n"',
        lambda _match: '"PO-Revision-Date: YEAR-MO-DA HO:MI+ZONE\\n"',
        text,
    )
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if running the update would change tracked catalogs",
    )
    args = parser.parse_args()
    PO_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="clipper-i18n-") as temporary:
        temporary_path = Path(temporary)
        new_pot = temporary_path / POT_FILE.name
        _run(
            "xgettext",
            "--language=Python",
            "--from-code=UTF-8",
            "--keyword=_",
            "--keyword=N_",
            "--keyword=ngettext:1,2",
            "--keyword=pgettext:1c,2",
            "--keyword=npgettext:1c,2,3",
            "--add-comments=Translators",
            "--check=ellipsis-unicode",
            "--sort-by-file",
            "--package-name=Clipper",
            f"--default-domain={DOMAIN}",
            "--output",
            str(new_pot),
            *_python_sources(),
        )
        _normalize_timestamps(new_pot)

        new_english = temporary_path / "en.po"
        _run("msgen", str(new_pot), "--output-file", str(new_english))
        _set_english_headers(new_english)
        _normalize_timestamps(new_english)
        english_text = new_english.read_text(encoding="utf-8")
        english_text = english_text.replace(
            '"PO-Revision-Date: YEAR-MO-DA HO:MI+ZONE\\n"',
            '"PO-Revision-Date: 2026-08-29 00:00+0300\\n"',
        )
        english_text = english_text.replace(
            '"Last-Translator: FULL NAME <EMAIL@ADDRESS>\\n"',
            '"Last-Translator: Clipper contributors\\n"',
        )
        english_text = english_text.replace(
            '"Language-Team: LANGUAGE <LL@li.org>\\n"',
            '"Language-Team: English\\n"',
        )
        new_english.write_text(english_text, encoding="utf-8")

        changed = []
        generated = {POT_FILE: new_pot, PO_DIR / "en.po": new_english}
        for destination, source in generated.items():
            old = destination.read_text(encoding="utf-8") if destination.exists() else None
            new = source.read_text(encoding="utf-8")
            if old != new:
                changed.append(destination)
                if not args.check:
                    destination.write_text(new, encoding="utf-8")

    if args.check and changed:
        for path in changed:
            print(f"translation catalog needs updating: {path.relative_to(REPO_ROOT)}")
        return 1

    if not args.check:
        for po_file in sorted(PO_DIR.glob("*.po")):
            if po_file.name == "en.po":
                continue
            _run(
                "msgmerge",
                "--update",
                "--backup=none",
                str(po_file),
                str(POT_FILE),
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
