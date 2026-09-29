"""Failure taxonomy for the provisioning pipeline.

Every stage failure is classified as transient (retry with jitter) or fatal
(dead selector, unrecoverable rejection, unsupported widget) — the pipeline
logs the class and either retries or moves to the next run.
"""

from __future__ import annotations


class PipelineError(Exception):
    """Base error carrying a failure classification."""

    classification = "fatal"


class TransientError(PipelineError):
    classification = "transient"


class FatalError(PipelineError):
    classification = "fatal"


class DeadSelectorError(FatalError):
    """A required element is absent after its wait window — the page
    structure no longer matches our selectors. Never blind-retry."""


class UnsupportedChallengeVariant(FatalError):
    """The embedded widget does not match any known signature. Carries a
    DOM snapshot artifact path for offline analysis."""

    def __init__(self, message: str, artifact_path: str | None = None):
        super().__init__(message)
        self.artifact_path = artifact_path


class ChallengeRejected(TransientError):
    """Widget rejected a single attempt; a retry with a fresh trajectory
    is appropriate."""


class ChallengeExhausted(FatalError):
    """Attempts budget for this run is spent."""


class VerificationTimeout(TransientError):
    """The verification message did not arrive in time."""


class PacingHalt(FatalError):
    """The single-lane pacer refused to schedule another run (daily cap or
    challenge-failure circuit breaker)."""
