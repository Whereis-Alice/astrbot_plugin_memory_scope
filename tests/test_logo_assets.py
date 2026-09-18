"""Regression checks for the plugin-card logo consumed by AstrBot."""

from pathlib import Path
import struct


ROOT = Path(__file__).resolve().parents[1]
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_header(path: Path) -> tuple[int, int, int, int]:
    data = path.read_bytes()
    assert data.startswith(PNG_SIGNATURE)
    assert data[12:16] == b"IHDR"
    width, height, bit_depth, color_type = struct.unpack(">IIBB", data[16:26])
    return width, height, bit_depth, color_type


def test_astrbot_plugin_logo_is_a_large_transparent_png():
    """AstrBot reads this exact root-level filename for its plugin card."""
    logo = ROOT / "logo.png"
    assert logo.is_file()
    assert png_header(logo) == (512, 512, 8, 6)
    assert logo.read_bytes() == (ROOT / "assets/logo/logo-512.png").read_bytes()


def test_logo_source_and_small_size_exports_are_present():
    assert (ROOT / "assets/logo/logo.svg").is_file()
    for size in (64, 128, 512):
        assert png_header(ROOT / f"assets/logo/logo-{size}.png") == (
            size,
            size,
            8,
            6,
        )
