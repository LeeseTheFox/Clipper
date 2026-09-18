"""Exercise the actual native-capture provider and scene gates from the patches."""


def test_native_capture_guards(run_native_test):
    run_native_test("native_capture.c", "native_capture_under_test.h", "-pthread")
