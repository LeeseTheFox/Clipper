# Translating Clipper

Clipper uses GNU gettext PO files. It discovers compiled catalogs automatically,
so adding a language does not require application code changes.

## Add a language

1. Copy `clipper.pot` to `<locale>.po`, such as `uk.po` or `pt_BR.po`.
2. Set the PO header's `Language` field to that locale.
3. Set `X-Clipper-Language-Name` to the language's own name, such as `Українська`.
4. Set `X-Clipper-Text-Direction` to `ltr` or `rtl`.
5. Translate every entry. Preserve named placeholders such as `%(name)s`.
6. From the repository root, run
   `./venv/bin/python tools/build_translations.py` to validate and compile
   the catalogs for a development run.

The language picker reads the language name and locale from its catalog.
Flatpak builds compile catalogs into
`/app/share/locale/<locale>/LC_MESSAGES/`.

## Update translations

After changing user-facing text, run
`./venv/bin/python tools/update_translations.py` from the repository root.
Translate all new or changed entries in every non-English PO file, then commit
the updated POT and PO files together.
