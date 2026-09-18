"""Keep actionable aggregate recorder results visible without verbose OBS logs."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_direct_vaapi_teardown_summaries_reach_normal_logs():
    source = (ROOT / "engine/src/clipper_engine.c").read_text(encoding="utf-8")
    assert 'strstr(msg, "Clipper VAAPI direct:")' in source
    assert 'strstr(msg, "Clipper VAAPI direct imports:")' in source
    assert "g_verbose || direct_vaapi_summary" in source
