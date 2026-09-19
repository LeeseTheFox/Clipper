"""Exercise the actual native-capture provider and scene gates from the patches."""


def test_native_capture_guards(run_native_test):
    run_native_test("native_capture.c", "native_capture_under_test.h", "-pthread")


def test_idle_render_preserves_consumers(run_native_test):
    run_native_test("idle_render.c", "idle_render_under_test.h", "-pthread")
