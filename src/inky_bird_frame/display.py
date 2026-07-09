"""Pimoroni Inky display adapter."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import Protocol, cast

from .errors import MissingDependencyError


class _InkyDisplay(Protocol):
    width: int
    height: int

    def set_image(self, image: object) -> None: ...

    def show(self) -> None: ...


def show_on_inky(image_path: Path) -> tuple[int, int]:
    try:
        from PIL import Image
    except ModuleNotFoundError as exc:
        raise MissingDependencyError("Pillow is required to load display images") from exc
    try:
        module = import_module("inky.auto")
    except ModuleNotFoundError as exc:
        raise MissingDependencyError("Pimoroni Inky is required for display output") from exc

    image = Image.open(image_path).convert("RGB")
    auto = cast(Callable[[], _InkyDisplay], module.auto)
    display = auto()
    expected_size = (display.width, display.height)
    if image.size != expected_size:
        raise ValueError(f"image size {image.size} does not match display size {expected_size}")
    display.set_image(image)
    display.show()
    return expected_size
