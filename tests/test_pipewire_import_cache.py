"""Run the packaged PipeWire cache code with counted GPU imports and fences."""

import pytest


@pytest.mark.parametrize("explicit_sync", [0, 1])
def test_pipewire_import_lifetime(run_native_test, explicit_sync):
    run_native_test(
        "pipewire_import_cache.c", "pipewire_cache_under_test.h",
        f"-DEXPLICIT_SYNC={explicit_sync}",
    )
