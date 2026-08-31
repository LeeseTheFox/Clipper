"""Best-effort game icon discovery from executable files."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

EXECUTABLE_SUFFIXES = (".exe", ".x86_64", ".x86", ".appimage")
SUPPORTED_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
MAX_EXECUTABLE_SEARCH_DEPTH = 4
MAX_GAME_ICON_CACHE_ENTRIES = 256
MAX_GAME_ICON_CACHE_BYTES = 32 * 1024 * 1024
IGNORED_EXECUTABLE_MARKERS = (
    "crashhandler",
    "unitycrashhandler",
    "installer",
    "redist",
    "setup",
    "unins",
    "vc_redist",
)
IGNORED_EXECUTABLE_SUFFIXES = (".dll", ".so", ".dylib")
_ICON_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="clipper-game-icon")


def icon_path_for_game(game_data: dict) -> str:
    """Return an icon path for *game_data*, doing discovery/extraction if needed."""
    cached_icon = cached_icon_path_for_game(game_data)
    if cached_icon:
        return cached_icon

    _executable_path, icon_path = resolve_icon_for_game(game_data)
    return icon_path


def cached_icon_path_for_game(game_data: dict) -> str:
    """Return a ready-to-display cached icon path without scanning or extraction."""
    existing_icon = _existing_file(game_data.get("icon_path"))
    if existing_icon and existing_icon.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES:
        return str(existing_icon)

    for key in ("executable_path", "path"):
        executable = _existing_file(game_data.get(key))
        if executable and _is_executable_candidate(executable):
            return cached_icon_path_for_executable(executable)

    return ""


def resolve_icon_for_game(game_data: dict) -> tuple[str, str]:
    """Return ``(executable_path, icon_path)`` after best-effort icon extraction."""
    executable = executable_path_for_game(game_data)
    if executable is None:
        return "", ""

    return str(executable), icon_path_for_executable(executable)


def submit_icon_resolution(game_data: dict):
    """Resolve a game icon on the shared background worker pool."""
    return _ICON_EXECUTOR.submit(resolve_icon_for_game, dict(game_data))


def executable_path_for_game(game_data: dict) -> Path | None:
    """Return the best executable path we can infer for a whitelist/game entry."""
    for key in ("executable_path", "path"):
        candidate = _existing_file(game_data.get(key))
        if candidate and _is_executable_candidate(candidate):
            return candidate

    install_path = game_data.get("install_path")
    if not isinstance(install_path, str) or not install_path.strip():
        return None

    path = Path(install_path).expanduser()
    if path.is_file() and _is_executable_candidate(path):
        return path
    if not path.is_dir():
        return None

    return _best_executable_in_directory(path, str(game_data.get("name") or ""))


def icon_path_for_executable(executable: Path) -> str:
    """Extract and cache a PNG icon from a Windows executable when possible."""
    executable = executable.expanduser()
    if not executable.is_file() or executable.suffix.lower() != ".exe":
        return ""

    cache_path = _cached_icon_path(executable)
    if cache_path.is_file():
        return str(cache_path)

    wrestool = shutil.which("wrestool")
    icotool = shutil.which("icotool")
    if not wrestool or not icotool:
        return ""

    with tempfile.TemporaryDirectory(prefix="clipper-game-icon-") as tmp:
        tmp_path = Path(tmp)
        ico_path = tmp_path / "icon.ico"

        try:
            result = subprocess.run(
                [wrestool, "--extract", "--type=14", str(executable)],
                check=False,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return ""

        if result.returncode != 0 or not result.stdout:
            return ""

        try:
            ico_path.write_bytes(result.stdout)
        except OSError:
            return ""

        try:
            result = subprocess.run(
                [icotool, "--extract", "--output", str(tmp_path), str(ico_path)],
                check=False,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return ""

        if result.returncode != 0:
            return ""

        extracted_icon = _largest_file(tmp_path.glob("*.png"))
        if extracted_icon is None:
            return ""

        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(extracted_icon, cache_path)
        except OSError:
            return ""

        _prune_icon_cache(cache_path.parent, protected=cache_path)
        if not cache_path.is_file():
            return ""

        return str(cache_path)


def cached_icon_path_for_executable(executable: Path) -> str:
    """Return the existing cached PNG for *executable*, or an empty string."""
    executable = executable.expanduser()
    if not executable.is_file() or executable.suffix.lower() != ".exe":
        return ""

    cache_path = _cached_icon_path(executable)
    return str(cache_path) if cache_path.is_file() else ""


def _best_executable_in_directory(directory: Path, game_name: str) -> Path | None:
    best_path = None
    best_score = -1
    for child in _iter_candidate_files(directory):
        if not child.is_file() or not _is_executable_candidate(child):
            continue
        if _should_ignore_executable(child.name):
            continue

        score = _executable_score(child.name, directory.name, game_name)
        score -= _relative_depth(child, directory)
        if score > best_score:
            best_path = child
            best_score = score

    return best_path


def _iter_candidate_files(directory: Path):
    pending = [(directory, 0)]
    while pending:
        current, depth = pending.pop(0)
        try:
            children = list(current.iterdir())
        except OSError:
            continue

        for child in children:
            if child.is_dir() and depth < MAX_EXECUTABLE_SEARCH_DEPTH:
                pending.append((child, depth + 1))
            elif child.is_file():
                yield child


def _relative_depth(path: Path, root: Path) -> int:
    try:
        return len(path.relative_to(root).parents) - 1
    except ValueError:
        return 0


def _is_executable_candidate(path: Path) -> bool:
    name = path.name.lower()
    if name.endswith(EXECUTABLE_SUFFIXES):
        return True
    return not path.suffix and os.access(path, os.X_OK)


def _should_ignore_executable(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith(IGNORED_EXECUTABLE_SUFFIXES) or any(
        marker in lowered for marker in IGNORED_EXECUTABLE_MARKERS
    )


def _executable_score(executable_name: str, install_dir_name: str, game_name: str) -> int:
    executable_stem = _normalize_executable_stem(executable_name)
    install_dir = _normalize_match_term(install_dir_name)
    game = _normalize_match_term(game_name)

    score = 0
    if executable_stem and executable_stem == install_dir:
        score += 100
    elif executable_stem and (executable_stem in install_dir or install_dir in executable_stem):
        score += 70

    if executable_stem and executable_stem == game:
        score += 90
    elif executable_stem and game and (executable_stem in game or game in executable_stem):
        score += 60

    if executable_name.lower().endswith(EXECUTABLE_SUFFIXES):
        score += 10

    return score


def _normalize_executable_stem(value: str) -> str:
    stem = value.lower()
    for suffix in (".x86_64", ".appimage", ".exe", ".x86"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return _normalize_match_term(stem)


def _normalize_match_term(value: str) -> str:
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def _existing_file(value: object) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    return path if path.is_file() else None


def _cached_icon_path(executable: Path) -> Path:
    cache_root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    digest_source = f"{executable}:{executable.stat().st_mtime_ns}:{executable.stat().st_size}"
    digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:24]
    return cache_root / "clipper" / "game-icons" / f"{digest}.png"


def _prune_icon_cache(cache_dir: Path, protected: Path | None = None) -> None:
    """Bound extracted game icons by entry count and total bytes."""
    try:
        entries = []
        for path in cache_dir.iterdir():
            if not path.is_file() or path.suffix.casefold() != ".png":
                continue
            stat = path.stat()
            entries.append((path == protected, stat.st_mtime_ns, stat.st_size, path))
    except OSError:
        return

    entries.sort(key=lambda item: (item[0], item[1], str(item[3])), reverse=True)
    total_bytes = 0
    for index, (_is_protected, _mtime_ns, size, path) in enumerate(entries):
        keep = (
            index < MAX_GAME_ICON_CACHE_ENTRIES
            and total_bytes + size <= MAX_GAME_ICON_CACHE_BYTES
        )
        if keep:
            total_bytes += size
            continue
        try:
            path.unlink()
        except OSError:
            pass


def _largest_file(paths) -> Path | None:
    largest_path = None
    largest_size = -1
    for path in paths:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > largest_size:
            largest_path = path
            largest_size = size
    return largest_path
