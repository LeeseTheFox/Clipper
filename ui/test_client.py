#!/usr/bin/env python3
"""
Simple test script for the engine client.

Usage:
    1. Start the engine in one terminal:
       ./build/engine/clipper-engine --socket /tmp/test-clipper.sock --output-dir /tmp/clips

    2. Run this script in another terminal:
       python3 ui/test_client.py
"""

import sys

import gi

gi.require_version("Gtk", "4.0")

from engine_client import EngineClient
from gi.repository import GLib

# Test socket path (adjust if engine uses different path)
SOCKET_PATH = "/tmp/test-clipper.sock"


def test_get_status(client: EngineClient) -> None:
    """Test get_status command."""
    print("Testing get_status...")

    def on_response(response: dict) -> None:
        print(f"  Response: {response}")
        if response.get("ok"):
            print("  ✓ get_status succeeded")
        else:
            print(f"  ✗ get_status failed: {response.get('error')}")

    client.get_status(on_response)


def test_save_clip(client: EngineClient) -> None:
    """Test save_replay_buffer command."""
    print("Testing save_replay_buffer...")

    def on_response(response: dict) -> None:
        print(f"  Response: {response}")
        if response.get("ok"):
            print("  ✓ save_replay_buffer succeeded")
        else:
            print(f"  ✗ save_replay_buffer failed: {response.get('error')}")

    client.save_replay_buffer(on_response)


def test_events(client: EngineClient) -> None:
    """Test event subscription."""
    print("Subscribing to events...")

    client.on("clip_saved", lambda data: print(f"  Event: clip_saved - {data}"))
    client.on("game_connected", lambda data: print(f"  Event: game_connected - {data}"))
    print("  ✓ Event handlers registered")


def on_error(message: str) -> None:
    """Handle errors."""
    print(f"Error: {message}")


def run_tests() -> None:
    """Run the test suite."""
    print(f"Connecting to engine at {SOCKET_PATH}...\n")

    client = EngineClient(SOCKET_PATH)
    client.on_error(on_error)

    if not client.connect():
        print("\nFailed to connect. Make sure the engine is running:")
        print(f"  ./build/engine/clipper-engine --socket {SOCKET_PATH} --output-dir /tmp/clips")
        sys.exit(1)

    print("✓ Connected to engine\n")

    # Register event handlers
    test_events(client)
    print()

    # Test commands with delays to let each complete
    test_get_status(client)

    # Wait a bit then test save
    GLib.timeout_add(1000, lambda: (test_save_clip(client), False))

    # Disconnect after a few seconds
    GLib.timeout_add(
        3000,
        lambda: (
            print("\n✓ Tests complete, disconnecting..."),
            client.disconnect(),
            main_loop.quit(),
            False,
        ),
    )

    # Run the GLib main loop
    global main_loop
    main_loop = GLib.MainLoop()
    try:
        main_loop.run()
    except KeyboardInterrupt:
        print("\nInterrupted")
        client.disconnect()


if __name__ == "__main__":
    run_tests()
