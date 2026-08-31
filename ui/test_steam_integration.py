#!/usr/bin/env python3
"""
Test script for Steam integration in Clipper whitelist.

This tests the Steam library detection and game discovery
without requiring the full GTK UI.
"""

import sys
from pathlib import Path

# Add parent directory to path so we can import the modules
sys.path.insert(0, str(Path(__file__).parent))

import steam


def test_steam_detection():
    """Test Steam root detection"""
    print("Testing Steam detection...")
    steam_root = steam.find_steam_root()

    if steam_root is None:
        print("❌ Steam not found at ~/.local/share/Steam")
        print("   This is expected if Steam is not installed.")
        return False
    else:
        print(f"✓ Steam found at: {steam_root}")
        return True


def test_game_discovery(steam_root):
    """Test installed game discovery"""
    print("\nTesting game discovery...")
    games = steam.get_installed_games(steam_root)

    if not games:
        print("❌ No games found")
        print("   This could mean:")
        print("   - No games are installed")
        print("   - libraryfolders.vdf is missing or malformed")
        return

    print(f"✓ Found {len(games)} installed game(s):\n")

    # Show first 10 games
    for game in games[:10]:
        print(f"  • {game.name}")
        print(f"    App ID: {game.appid}")
        print(f"    Path: {game.install_path}")
        print()

    if len(games) > 10:
        print(f"  ... and {len(games) - 10} more games")


def test_launch_options(steam_root):
    """Test reading launch options (non-destructive)"""
    print("\nTesting launch options reading...")

    # Try to read launch options for a common game (CS2 - appid 730)
    # This is read-only, so it's safe to test
    test_appid = "730"  # Counter-Strike 2

    try:
        options = steam.get_launch_options(test_appid, steam_root)
        if options:
            print(f"✓ Launch options for appid {test_appid}: {options}")
        else:
            print(f"✓ No launch options set for appid {test_appid}")
    except FileNotFoundError as e:
        print(f"⚠ Could not read launch options: {e}")
        print("  This is expected if localconfig.vdf is not found")
    except Exception as e:
        print(f"❌ Error reading launch options: {e}")


def test_steam_running():
    """Test Steam process detection"""
    print("\nTesting Steam process detection...")

    # This uses the private function, but we can test it
    is_running = steam._is_steam_running()

    if is_running:
        print("✓ Steam is currently running")
        print("  NOTE: Cannot safely modify launch options while Steam is running")
    else:
        print("✓ Steam is not running")
        print("  Safe to modify launch options")


def main():
    """Run all tests"""
    print("=" * 60)
    print("Clipper Steam Integration Test")
    print("=" * 60)
    print()

    # Test 1: Steam detection
    steam_found = test_steam_detection()

    if not steam_found:
        print("\n" + "=" * 60)
        print("Cannot continue without Steam installation")
        print("=" * 60)
        return

    steam_root = steam.find_steam_root()

    # Test 2: Game discovery
    test_game_discovery(steam_root)

    # Test 3: Launch options (read-only)
    test_launch_options(steam_root)

    # Test 4: Steam process detection
    test_steam_running()

    print("\n" + "=" * 60)
    print("Testing complete!")
    print("=" * 60)
    print()
    print("To test the full UI integration:")
    print("  1. Close Steam if it's running")
    print("  2. Run: ./run.sh")
    print("  3. Navigate to the Whitelist tab")
    print("  4. Click 'Add from Steam'")
    print("  5. Select a game and add it")
    print("  6. Verify the game appears in the whitelist")
    print("  7. Check Steam launch options for the game")
    print()


if __name__ == "__main__":
    main()
