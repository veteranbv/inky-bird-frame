"""Image sizing and orientation helpers."""

from __future__ import annotations

from pathlib import Path

from .config import DisplayConfig
from .errors import MissingDependencyError

DEFAULT_DISPLAY_CONFIG = DisplayConfig()


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
    config: DisplayConfig = DEFAULT_DISPLAY_CONFIG,
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
    portrait = Image.new("RGB", config.portrait_size, paper_color)
    fitted = ImageOps.contain(image, config.portrait_size, Image.Resampling.LANCZOS)
    portrait.paste(
        fitted,
        (
            (config.portrait_size[0] - fitted.width) // 2,
            (config.portrait_size[1] - fitted.height) // 2,
        ),
    )

    portrait_path = output_dir / f"{source_path.stem}-portrait.png"
    display_path = output_dir / f"{source_path.stem}-display.png"
    portrait.save(portrait_path)
    portrait.rotate(config.rotation_degrees, expand=True).save(display_path)
    return portrait_path, display_path


def prepare_generated_plate(
    source_path: Path,
    portrait_path: Path,
    display_path: Path,
    *,
    config: DisplayConfig = DEFAULT_DISPLAY_CONFIG,
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
            config.portrait_size,
            method=Image.Resampling.LANCZOS,
        )
        portrait_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(portrait_path, format="PNG")
        display = image.rotate(config.rotation_degrees, expand=True)
        if display.size != config.hardware_size:
            raise ValueError(
                f"rotated image size {display.size} does not match {config.hardware_size}"
            )
        display.save(display_path, format="PNG")
