import ast
import json
import subprocess
from pathlib import Path

import i18n

REPO_ROOT = Path(__file__).resolve().parent.parent


def _write_catalog(path: Path, *, name: str, direction: str = "ltr") -> None:
    path.write_text(
        'msgid ""\n'
        'msgstr ""\n'
        f'"Language: {path.stem}\\n"\n'
        f'"X-Clipper-Language-Name: {name}\\n"\n'
        f'"X-Clipper-Text-Direction: {direction}\\n"\n',
        encoding="utf-8",
    )


def test_available_languages_are_discovered_from_catalogs(monkeypatch, tmp_path):
    _write_catalog(tmp_path / "uk.po", name="Українська")
    _write_catalog(tmp_path / "ar.po", name="العربية", direction="rtl")
    monkeypatch.setattr(i18n, "source_po_directory", lambda: tmp_path)
    monkeypatch.setattr(i18n, "locale_directory", lambda: tmp_path / "compiled")

    languages = {language.code: language for language in i18n.available_languages()}

    assert languages["en"].name == "English"
    assert languages["uk"].name == "Українська"
    assert languages["ar"].direction == "rtl"


def test_system_language_uses_supported_base_locale_or_english(monkeypatch, tmp_path):
    _write_catalog(tmp_path / "uk.po", name="Українська")
    monkeypatch.setattr(i18n, "source_po_directory", lambda: tmp_path)
    monkeypatch.setattr(i18n, "locale_directory", lambda: tmp_path / "compiled")

    assert i18n.effective_system_language({"LANG": "uk_UA.UTF-8"}).code == "uk"
    assert i18n.effective_system_language({"LANG": "fr_FR.UTF-8"}).code == "en"


def test_saved_language_is_read_before_ui_import(monkeypatch, tmp_path):
    config_dir = tmp_path / "clipper"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"ui_language": "uk"}), encoding="utf-8"
    )

    assert (
        i18n.configured_language_from_disk({"XDG_CONFIG_HOME": str(tmp_path)})
        == "uk"
    )


def test_translation_catalogs_are_current_and_valid():
    subprocess.run(
        [str(REPO_ROOT / "venv" / "bin" / "python"), "tools/update_translations.py", "--check"],
        cwd=REPO_ROOT,
        check=True,
    )
    subprocess.run(
        [str(REPO_ROOT / "venv" / "bin" / "python"), "tools/build_translations.py"],
        cwd=REPO_ROOT,
        check=True,
    )


def test_direct_widget_text_is_not_a_bare_literal():
    """Guard the common GTK sinks while allowing protocol/config literals."""
    first_argument_sinks = {
        "set_accept_label",
        "set_body",
        "set_button_label",
        "set_description",
        "set_heading",
        "set_label",
        "set_placeholder_text",
        "set_subtitle",
        "set_title",
        "set_tooltip_text",
    }
    failures = []
    for path in sorted((REPO_ROOT / "ui").glob("*.py")):
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            name = call.func.attr if isinstance(call.func, ast.Attribute) else ""
            argument = None
            if name in first_argument_sinks and call.args:
                argument = call.args[0]
            elif name == "add_response" and len(call.args) > 1:
                argument = call.args[1]
            elif name == "update_property" and len(call.args) > 1:
                argument = call.args[1]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                if any(character.isalpha() for character in argument.value):
                    failures.append(f"{path.relative_to(REPO_ROOT)}:{call.lineno}")
            if isinstance(argument, (ast.List, ast.Tuple)):
                for item in argument.elts:
                    if isinstance(item, ast.Constant) and isinstance(item.value, str):
                        if any(character.isalpha() for character in item.value):
                            failures.append(
                                f"{path.relative_to(REPO_ROOT)}:{call.lineno}"
                            )
    assert failures == []
