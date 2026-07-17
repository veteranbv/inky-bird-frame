"""Pimoroni Inky display adapter."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import Protocol, cast

from .errors import MissingDependencyError
from .images import HARDWARE_SIZE, PAPER_COLOR, SUPPORTED_HARDWARE_SIZES


class InkyDisplay(Protocol):
    width: int
    height: int

    def set_image(self, image: object) -> None: ...

    def show(self) -> None: ...


def detect_inky_display() -> InkyDisplay:
    """Auto-detect and validate a supported Pimoroni Inky panel."""

    try:
        module = import_module("inky.auto")
    except ModuleNotFoundError as exc:
        raise MissingDependencyError("Pimoroni Inky is required for display output") from exc

    auto = cast(Callable[[], InkyDisplay], module.auto)
    display = auto()
    size = (display.width, display.height)
    if size not in SUPPORTED_HARDWARE_SIZES:
        supported = ", ".join(
            f"{width}x{height}" for width, height in sorted(SUPPORTED_HARDWARE_SIZES)
        )
        raise ValueError(f"unsupported Inky display size {size}; supported sizes: {supported}")
    return display


def _fit_canonical_image(image: object, display_size: tuple[int, int]) -> object:
    from PIL import Image, ImageOps

    if not isinstance(image, Image.Image):
        raise TypeError("display image must be a Pillow image")
    if image.size == display_size:
        return image
    if image.size != HARDWARE_SIZE:
        raise ValueError(
            f"image size {image.size} does not match canonical size {HARDWARE_SIZE} "
            f"or display size {display_size}"
        )

    fitted = ImageOps.contain(image, display_size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", display_size, PAPER_COLOR)
    canvas.paste(
        fitted,
        (
            (display_size[0] - fitted.width) // 2,
            (display_size[1] - fitted.height) // 2,
        ),
    )
    return canvas


def show_on_inky(
    image_path: Path,
    *,
    display: InkyDisplay | None = None,
) -> tuple[int, int]:
    try:
        from PIL import Image
    except ModuleNotFoundError as exc:
        raise MissingDependencyError("Pillow is required to load display images") from exc
    image = Image.open(image_path).convert("RGB")
    active_display = display if display is not None else detect_inky_display()
    expected_size = (active_display.width, active_display.height)
    if expected_size not in SUPPORTED_HARDWARE_SIZES:
        raise ValueError(f"unsupported Inky display size {expected_size}")
    active_display.set_image(_fit_canonical_image(image, expected_size))
    active_display.show()
    return expected_size
