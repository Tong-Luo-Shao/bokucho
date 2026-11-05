"""Typed application failures safe for CLI and GUI presentation."""


class AppError(Exception):
    code = "application_error"


class ValidationFailure(AppError):
    code = "validation"


class InvalidProject(AppError):
    code = "invalid_project"


class MissingCredential(AppError):
    code = "missing_credential"


class MissingDependency(AppError):
    code = "missing_dependency"


class OcrRuntimeLost(MissingDependency):
    """The OCR worker can no longer answer any subsequent request."""


class MultiFrameImage(AppError):
    code = "multi_frame_image"


class ProviderFailure(AppError):
    code = "provider_failure"


class ProviderRefused(ProviderFailure):
    """The provider declined to answer this text at all."""

    code = "provider_refused"


class ProviderMiscounted(ProviderFailure):
    """The answer held more or fewer translations than lines were asked."""

    code = "provider_miscounted"


class ProviderRateLimited(ProviderFailure):
    """The provider refused because a quota is spent, not because of the text."""

    code = "provider_rate_limited"

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ReleaseBlocked(AppError):
    code = "release_blocked"


class JobBusy(AppError):
    code = "job_busy"


class JobCancelled(AppError):
    code = "cancelled"
