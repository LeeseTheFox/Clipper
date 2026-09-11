"""Metadata and executable edits for configured games."""

import os
from pathlib import Path

import steam
from i18n import _
from process_watcher import entries_share_executable_identity


def steam_details(entry: dict) -> tuple[str, str, str]:
    """Resolve only the saved installation/account; never select another account."""
    environment = str(entry.get("steam_environment") or "")
    account_id = str(entry.get("steam_account_id") or "")
    account_label = account_id or _("Not available")
    root = ""
    try:
        installation = next(
            (
                item
                for item in steam.discover_steam_installations()
                if item.stable_id == entry.get("steam_installation")
            ),
            None,
        )
        if installation is not None:
            root = str(installation.data_root)
            environment = installation.environment.value
            if account_id:
                account = steam.select_steam_account(installation.data_root, account_id)
                if account.account_name != account_id:
                    account_label = f"{account.account_name} ({account_id})"
    except (OSError, ValueError, steam.AmbiguousSteamAccountError):
        pass
    source = {
        "native_steam": _("Native Steam"),
        "flatpak_steam_user": _("Flatpak Steam (user)"),
        "flatpak_steam_system": _("Flatpak Steam (system)"),
        "steam_snap": _("Steam Snap"),
    }.get(environment, _("Not available"))
    return source, account_label, root or _("Not available")


def validated_executable_path(value: str) -> Path:
    """Validate a file selected locally, not a path observed in another sandbox."""
    path = Path(value.strip()).expanduser()
    if (
        not value.strip()
        or not path.is_absolute()
        or not path.is_file()
        or not (os.access(path, os.X_OK) or path.suffix.lower() == ".exe")
    ):
        raise ValueError(_("Choose an existing executable file using its full path."))
    return path.resolve()


def replace_executable(entries: list[dict], original: dict, value: str) -> list[dict]:
    """Update matching fields together, retaining concurrent unrelated metadata."""
    if "appid" in original:
        raise ValueError(_("Steam game executables cannot be edited here."))
    path = validated_executable_path(value)

    def identity(entry):
        return (
            entry.get("path") or entry.get("executable_path"),
            entry.get("flatpak_id", ""),
            entry.get("process_name", ""),
        )

    matches = [
        index
        for index, entry in enumerate(entries)
        if "appid" not in entry and identity(entry) == identity(original)
    ]
    if len(matches) != 1:
        raise ValueError(_("This game has changed. Close its details and try again."))
    index = matches[0]
    updated = dict(entries[index])
    old_name = Path(str(updated.get("path") or "")).name
    updated.update(path=str(path), executable_path=str(path), executable_name=path.name)
    if not updated.get("name") or updated.get("name") == old_name:
        updated["name"] = path.name
    if str(path) != str(original.get("executable_path") or original.get("path") or ""):
        for key in (
            "icon_path",
            "audio_process",
            "install_path",
            "flatpak_id",
            "process_name",
            "match_mode",
        ):
            updated.pop(key, None)
        if path.suffix.lower() != ".exe":
            updated["match_mode"] = "executable"
    if any(
        entries_share_executable_identity(entry, updated)
        for other, entry in enumerate(entries)
        if other != index
    ):
        raise ValueError(_("This executable is already in the Games list."))
    result = list(entries)
    result[index] = updated
    return result
