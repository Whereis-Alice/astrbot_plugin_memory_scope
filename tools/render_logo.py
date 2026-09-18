"""Render the checked-in MemoryScope SVG into AstrBot-friendly PNG sizes."""

from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets/logo/logo.svg"
OUTPUTS = {
    ROOT / "logo.png": 512,
    ROOT / "assets/logo/logo-512.png": 512,
    ROOT / "assets/logo/logo-128.png": 128,
    ROOT / "assets/logo/logo-64.png": 64,
}


def main() -> None:
    magick = shutil.which("magick")
    if not magick:
        raise SystemExit("ImageMagick 7 (magick) is required to render the logo PNGs.")
    for destination, size in OUTPUTS.items():
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                magick,
                "-background",
                "none",
                "-depth",
                "8",
                str(SOURCE),
                "-resize",
                f"{size}x{size}",
                str(destination),
            ],
            check=True,
        )
        print(f"{destination.relative_to(ROOT)} {size}x{size}")


if __name__ == "__main__":
    main()
