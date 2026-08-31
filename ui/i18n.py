"""Gettext bootstrap and language discovery for Clipper.

This module intentionally has no GTK imports.  ``bootstrap()`` is called before
GTK is imported so both Clipper and the toolkit observe the selected locale.
"""

from __future__ import annotations

import ast
import gettext
import json
import locale
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DOMAIN = "io.github.leesethefox.Clipper"
SYSTEM_LANGUAGE = "system"
ENGLISH_LANGUAGE = "en"
_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:_[A-Za-z]{2,4})?(?:@[A-Za-z0-9_-]+)?$")
_SYSTEM_LANGUAGE_ENV = "CLIPPER_INTERNAL_SYSTEM_LANGUAGE"

if _SYSTEM_LANGUAGE_ENV not in os.environ:
    os.environ[_SYSTEM_LANGUAGE_ENV] = json.dumps(os.environ.get("LANGUAGE"))


@dataclass(frozen=True)
class Language:
    """One language that can be selected in Clipper."""

    code: str
    name: str
    direction: str = "ltr"


_translation: gettext.NullTranslations = gettext.NullTranslations()
_selected_language = SYSTEM_LANGUAGE
_effective_language = ENGLISH_LANGUAGE
_effective_direction = "ltr"


def _(message: str) -> str:
    """Translate a message in Clipper's active catalog."""
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, number: int) -> str:
    """Translate a plural message in Clipper's active catalog."""
    return _translation.ngettext(singular, plural, number)


def pgettext(context: str, message: str) -> str:
    """Translate a context-qualified message."""
    return _translation.pgettext(context, message)


def npgettext(context: str, singular: str, plural: str, number: int) -> str:
    """Translate a context-qualified plural message."""
    return _translation.npgettext(context, singular, plural, number)


def N_(message: str) -> str:  # noqa: N802 - conventional gettext marker
    """Mark a message for extraction while deferring its translation."""
    return message


def locale_directory() -> Path:
    """Return the installed or development gettext catalog directory."""
    overridden = os.environ.get("CLIPPER_LOCALEDIR")
    if overridden:
        return Path(overridden)
    module_dir = Path(__file__).resolve().parent
    if module_dir == Path("/app/share/clipper/ui"):
        return Path("/app/share/locale")
    return module_dir.parent / "build" / "locale"


def source_po_directory() -> Path | None:
    """Return the source PO directory when running from a checkout."""
    candidate = Path(__file__).resolve().parent.parent / "po"
    return candidate if candidate.is_dir() else None


def _normalize_language(value: object) -> str:
    text = str(value or "").strip().replace("-", "_")
    text = text.split(".", 1)[0]
    if text in {"C", "POSIX", "C_UTF_8", "C_utf8"}:
        return ENGLISH_LANGUAGE
    return text if _LANGUAGE_RE.fullmatch(text) else ""


def _po_metadata(path: Path) -> dict[str, str]:
    """Read RFC-822 metadata from the header entry of a PO file."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    fragments: list[str] = []
    in_header = False
    for line in lines:
        if line == 'msgid ""':
            in_header = True
            continue
        if not in_header:
            continue
        if line.startswith("msgstr "):
            value = line.removeprefix("msgstr ").strip()
        elif line.startswith('"'):
            value = line
        elif fragments:
            break
        else:
            continue
        try:
            fragments.append(ast.literal_eval(value))
        except (SyntaxError, ValueError):
            return {}
    metadata: dict[str, str] = {}
    for line in "".join(fragments).splitlines():
        key, separator, value = line.partition(":")
        if separator:
            metadata[key.strip().lower()] = value.strip()
    return metadata


def _mo_metadata(path: Path) -> dict[str, str]:
    try:
        with path.open("rb") as catalog:
            return {
                str(key).lower(): str(value)
                for key, value in gettext.GNUTranslations(catalog).info().items()
            }
    except (OSError, EOFError):
        return {}


def available_languages() -> tuple[Language, ...]:
    """Discover languages from installed MO files and checkout PO files."""
    metadata_by_code: dict[str, dict[str, str]] = {
        ENGLISH_LANGUAGE: {
            "x-clipper-language-name": "English",
            "x-clipper-text-direction": "ltr",
        }
    }
    locale_dir = locale_directory()
    if locale_dir.is_dir():
        for path in locale_dir.glob(f"*/LC_MESSAGES/{DOMAIN}.mo"):
            code = _normalize_language(path.parent.parent.name)
            if code:
                metadata_by_code[code] = _mo_metadata(path)
    po_dir = source_po_directory()
    if po_dir is not None:
        for path in po_dir.glob("*.po"):
            code = _normalize_language(path.stem)
            if code:
                metadata_by_code.setdefault(code, _po_metadata(path))

    languages = []
    for code, metadata in metadata_by_code.items():
        default_name = "English" if code == ENGLISH_LANGUAGE else code
        name = metadata.get("x-clipper-language-name", default_name)
        direction = metadata.get("x-clipper-text-direction", "ltr").lower()
        languages.append(Language(code, name, "rtl" if direction == "rtl" else "ltr"))
    return tuple(sorted(languages, key=lambda item: item.name.casefold()))


def _environment_language_names(environment: Mapping[str, str]) -> list[str]:
    for key in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        raw = environment.get(key, "")
        if raw:
            return [part for part in raw.split(":") if part]
    return [ENGLISH_LANGUAGE]


def _match_language(requested: str, languages: tuple[Language, ...]) -> Language | None:
    normalized = _normalize_language(requested)
    if not normalized:
        return None
    by_code = {language.code.casefold(): language for language in languages}
    exact = by_code.get(normalized.casefold())
    if exact is not None:
        return exact
    base = normalized.split("_", 1)[0].split("@", 1)[0]
    return by_code.get(base.casefold())


def effective_system_language(
    environment: Mapping[str, str] | None = None,
) -> Language:
    """Resolve the desktop language against Clipper's available catalogs."""
    languages = available_languages()
    source = environment if environment is not None else os.environ
    for requested in _environment_language_names(source):
        matched = _match_language(requested, languages)
        if matched is not None:
            return matched
    return next(language for language in languages if language.code == ENGLISH_LANGUAGE)


def configure(language: str, *, environment: Mapping[str, str] | None = None) -> Language:
    """Activate a language before GTK constructs any application UI."""
    global _effective_direction, _effective_language, _selected_language, _translation

    requested = _normalize_language(language)
    if language == SYSTEM_LANGUAGE:
        requested = SYSTEM_LANGUAGE
    if not requested:
        requested = SYSTEM_LANGUAGE

    original_environment = dict(environment if environment is not None else os.environ)
    if environment is None:
        try:
            original_language = json.loads(os.environ[_SYSTEM_LANGUAGE_ENV])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            original_language = None
        if isinstance(original_language, str):
            original_environment["LANGUAGE"] = original_language
        else:
            original_environment.pop("LANGUAGE", None)
    languages = available_languages()
    if requested == SYSTEM_LANGUAGE:
        effective = effective_system_language(original_environment)
        translation_languages = _environment_language_names(original_environment)
        if "LANGUAGE" in original_environment:
            os.environ["LANGUAGE"] = original_environment["LANGUAGE"]
        else:
            os.environ.pop("LANGUAGE", None)
    else:
        effective = _match_language(requested, languages)
        if effective is None:
            effective = next(item for item in languages if item.code == ENGLISH_LANGUAGE)
        translation_languages = [requested]
        os.environ["LANGUAGE"] = requested

    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        # The app catalog remains usable even when the host lacks locale data.
        pass

    localedir = str(locale_directory())
    gettext.bindtextdomain(DOMAIN, localedir)
    gettext.textdomain(DOMAIN)
    _translation = gettext.translation(
        DOMAIN,
        localedir=localedir,
        languages=translation_languages,
        fallback=True,
    )
    _selected_language = requested
    _effective_language = effective.code
    _effective_direction = effective.direction
    return effective


def configured_language_from_disk(environment: Mapping[str, str] | None = None) -> str:
    """Read only the language preference before importing the full UI."""
    source = environment if environment is not None else os.environ
    if source.get("CLIPPER_AGENT_TEST_MODE"):
        return ENGLISH_LANGUAGE
    overridden = source.get("CLIPPER_LANGUAGE")
    if overridden:
        return overridden
    config_home = source.get("XDG_CONFIG_HOME")
    config_path = (
        Path(config_home) / "clipper" / "config.json"
        if config_home
        else Path.home() / ".config" / "clipper" / "config.json"
    )
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return SYSTEM_LANGUAGE
    value = data.get("ui_language", SYSTEM_LANGUAGE) if isinstance(data, dict) else SYSTEM_LANGUAGE
    return str(value)


def bootstrap() -> Language:
    """Configure the saved language during application module import."""
    return configure(configured_language_from_disk())


def selected_language() -> str:
    return _selected_language


def effective_language() -> str:
    return _effective_language


def text_direction() -> str:
    return _effective_direction
