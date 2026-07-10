"""Project-specific exceptions."""


class InkyBirdFrameError(Exception):
    """Base exception for expected application failures."""


class DataSourceError(InkyBirdFrameError):
    """Raised when a remote data source cannot be used."""


class InsufficientReferencesError(DataSourceError):
    """Raised when a valid source response cannot satisfy reference policy."""


class UnsupportedSpeciesError(InkyBirdFrameError):
    """Raised when no renderer exists for the requested species."""


class MissingDependencyError(InkyBirdFrameError):
    """Raised when an optional runtime dependency is unavailable."""


class ConfigurationError(InkyBirdFrameError):
    """Raised when application configuration is missing or invalid."""


class CatalogError(InkyBirdFrameError):
    """Raised when catalog data is missing, invalid, or inconsistent."""


class CatalogPublishError(InkyBirdFrameError):
    """Raised when an approved catalog cannot be published safely."""


class GenerationError(InkyBirdFrameError):
    """Raised when Codex cannot produce or validate an artifact."""


class QualityReviewError(GenerationError):
    """Raised when a species exhausts its automated visual-review attempts."""
