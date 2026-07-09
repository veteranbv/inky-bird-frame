"""Image sizing and orientation helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from .errors import MissingDependencyError

PORTRAIT_SIZE: Final = (1200, 1600)
HARDWARE_SIZE: Final = (1600, 1200)
ROTATION_DEGREES: Final = 90


def slugify(value: str) -> str:
    chars: list[str] = []
    previous_dash = False
    for char in value.lower():
        if char.isalnum():
            chars.append(char)
            previous_dash = False
        elif not previous_dash:
            chars.append("-")
            previous_dash = True
    return "".join(chars).strip("-")


def prepare_uploaded_image(
    source_path: Path,
    output_dir: Path,
    *,
    paper_color: tuple[int, int, int] = (238, 222, 184),
) -> tuple[Path, Path]:
    try:
        from PIL import Image, ImageOps
    except ModuleNotFoundError as exc:
        raise MissingDependencyError("Pillow is required to prepare images") from exc

    if not source_path.exists():
        raise FileNotFoundError(source_path)

    output_dir.mkdir(parents=True, exist_ok=True)
    image = Image.open(source_path).convert("RGB")
    portrait = Image.new("RGB", PORTRAIT_SIZE, paper_color)
    fitted = ImageOps.contain(image, PORTRAIT_SIZE, Image.Resampling.LANCZOS)
    portrait.paste(
        fitted,
        (
            (PORTRAIT_SIZE[0] - fitted.width) // 2,
            (PORTRAIT_SIZE[1] - fitted.height) // 2,
        ),
    )

    portrait_path = output_dir / f"{source_path.stem}-portrait.png"
    display_path = output_dir / f"{source_path.stem}-display.png"
    portrait.save(portrait_path)
    portrait.rotate(ROTATION_DEGREES, expand=True).save(display_path)
    return portrait_path, display_path


def prepare_generated_plate(
    source_path: Path,
    portrait_path: Path,
    display_path: Path,
) -> None:
    try:
        from PIL import Image, ImageOps
    except ModuleNotFoundError as exc:
        raise MissingDependencyError("Pillow is required to prepare generated plates") from exc

    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    with Image.open(source_path) as source:
        image = ImageOps.fit(
            source.convert("RGB"),
            PORTRAIT_SIZE,
            method=Image.Resampling.LANCZOS,
        )
        portrait_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(portrait_path, format="PNG")
        display = image.rotate(ROTATION_DEGREES, expand=True)
        if display.size != HARDWARE_SIZE:
            raise ValueError(f"rotated image size {display.size} does not match {HARDWARE_SIZE}")
        display.save(display_path, format="PNG")
