"""Project-specific exceptions."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ProfileConflict


class InkyBirdFrameError(Exception):
    """Base exception for expected application failures."""


class DataSourceError(InkyBirdFrameError):
    """Raised when a remote data source cannot be used."""


class TaxonomyMatchError(DataSourceError):
    """Raised when a valid response has no unambiguous canonical species match."""


class InsufficientReferencesError(DataSourceError):
    """Raised when a valid source response cannot satisfy reference policy."""


class UnsupportedSpeciesError(InkyBirdFrameError):
    """Raised when no renderer exists for the requested species."""


class MissingDependencyError(InkyBirdFrameError):
    """Raised when an optional runtime dependency is unavailable."""


class ConfigurationError(InkyBirdFrameError):
    """Raised when application configuration is missing or invalid."""


class InstallationError(InkyBirdFrameError):
    """Raised when role setup cannot complete safely."""


class CatalogError(InkyBirdFrameError):
    """Raised when catalog data is missing, invalid, or inconsistent."""


class SpeciesStateError(CatalogError):
    """Raised when cached state for a single species is invalid."""


class CatalogPublishError(InkyBirdFrameError):
    """Raised when an approved catalog cannot be published safely."""


class GenerationError(InkyBirdFrameError):
    """Raised when Codex cannot produce or validate an artifact."""


class ResearchLimitError(GenerationError):
    """Raised when durable research capacity is unavailable until a known time."""

    def __init__(self, message: str, *, retry_at: datetime) -> None:
        super().__init__(message)
        self.retry_at = retry_at


class QualityReviewError(GenerationError):
    """Raised when a species exhausts its automated visual-review attempts."""

    def __init__(
        self,
        message: str,
        *,
        profile_conflicts: tuple[ProfileConflict, ...] | None = None,
    ) -> None:
        super().__init__(message)
        self.profile_conflicts = profile_conflicts
