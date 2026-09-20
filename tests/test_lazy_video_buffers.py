"""Exercise the allocation helpers shipped in the OBS patch."""

def test_lazy_video_allocation_contract(run_native_test):
    run_native_test("lazy_video_buffers.c", "lazy_buffers_under_test.h")
