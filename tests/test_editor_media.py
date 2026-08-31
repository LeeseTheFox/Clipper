from types import SimpleNamespace

from editor_media import normalize_probe


def test_probe_preserves_anamorphic_sample_aspect_ratio(tmp_path):
    clip = tmp_path / "anamorphic.mkv"
    data = {
        "format": {"duration": "5.0"},
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "width": 720,
                "height": 576,
                "sample_aspect_ratio": "64:45",
                "avg_frame_rate": "25/1",
                "r_frame_rate": "25/1",
            }
        ],
    }

    source = normalize_probe(
        clip,
        data,
        stat=SimpleNamespace(st_size=10, st_mtime_ns=20),
    )

    assert source.sample_aspect_ratio_num == 64
    assert source.sample_aspect_ratio_den == 45


def test_probe_defaults_invalid_sample_aspect_ratio_to_square_pixels(tmp_path):
    clip = tmp_path / "square-pixels.mkv"
    data = {
        "format": {"duration": "5.0"},
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "sample_aspect_ratio": "N/A",
            }
        ],
    }

    source = normalize_probe(
        clip,
        data,
        stat=SimpleNamespace(st_size=10, st_mtime_ns=20),
    )

    assert source.sample_aspect_ratio_num == 1
    assert source.sample_aspect_ratio_den == 1
