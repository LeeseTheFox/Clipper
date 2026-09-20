"""Compile actual added queue functions, not a separate behavioral model."""

def test_direct_surface_queue_contract(run_native_test):
    result = run_native_test("vaapi_direct_queue.c", "direct_queue_under_test.h", "-pthread")
    assert "direct surface queue tests passed" in result.stdout


def test_direct_surface_validation_cache(run_native_test):
    run_native_test("vaapi_direct_validation.c", "direct_validation_under_test.h")
