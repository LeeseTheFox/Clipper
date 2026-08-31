# Translating Clipper

Translation files live in this directory. Clipper uses standard GNU gettext
PO files and discovers every compiled catalog automatically; application code
does not need to be changed when a language is added.

To add a language:

1. Copy `clipper.pot` to `<locale>.po`, such as `uk.po` or `pt_BR.po`.
2. Set the PO header's `Language` field to that locale.
3. Add `X-Clipper-Language-Name` using the language's own name, such as
   `Українська`.
4. Add `X-Clipper-Text-Direction: ltr` or `rtl`.
5. Translate every entry. Keep named placeholders such as `%(name)s` intact.
6. Run `./venv/bin/python tools/build_translations.py` to validate and compile
   the catalogs for a development run.

After user-facing text changes, run
`./venv/bin/python tools/update_translations.py`, translate every newly added
entry in every non-English PO file, and commit the updated POT and PO files.

The language picker will include the new language based solely on its PO/MO
catalog. An installed Flatpak compiles catalogs into the standard
`/app/share/locale/<locale>/LC_MESSAGES/` location automatically.
