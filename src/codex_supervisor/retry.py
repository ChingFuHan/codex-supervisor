"""
Retry policy: pure function, no side effects.
Maps (job state, exit classification, rate limit event) -> RetryDecision.
"""

import datetime
import logging

from .models import ExitClassification, Job, RateLimitEvent, RetryDecision, SupervisorConfig

logger = logging.getLogger(__name__)

_MAX_BACKOFF_SECONDS = 4 * 3600  # 4 hours


def decide_retry(
    job: Job,
    classification: ExitClassification,
    rate_limit: RateLimitEvent | None,
    config: SupervisorConfig,
) -> RetryDecision:
    match classification:
        case ExitClassification.NORMAL_COMPLETION:
            return RetryDecision(False, None, "task completed successfully")

        case ExitClassification.USER_INTERRUPT:
            return RetryDecision(False, None, "user interrupted, not auto-resuming")

        case ExitClassification.RATE_LIMIT:
            return _rate_limit_decision(job, rate_limit, config)

        case ExitClassification.TRANSIENT_ERROR:
            return _crash_decision(job, config, max_retries=config.max_crash_retries)

        case ExitClassification.UNKNOWN_FAILURE:
            return _crash_decision(job, config, max_retries=config.max_unknown_retries)

        case _:
            return RetryDecision(False, None, f"unhandled classification: {classification}")


def _rate_limit_decision(
    job: Job,
    rate_limit: RateLimitEvent | None,
    config: SupervisorConfig,
) -> RetryDecision:
    now = datetime.datetime.now(datetime.timezone.utc)

    if rate_limit and rate_limit.reset_at:
        delta = (rate_limit.reset_at - now).total_seconds()
        delay = max(delta, 0.0)
        return RetryDecision(
            True,
            delay,
            f"rate limit: scheduling resume at {rate_limit.reset_at.isoformat()}",
        )

    # No reset time: exponential backoff on rate_limit_retries
    backoff = config.fallback_wait_minutes * 60 * (2 ** min(job.rate_limit_retries, 30))
    delay = min(backoff, config.max_wait_seconds)
    return RetryDecision(
        True,
        delay,
        f"rate limit (no reset time): fallback wait {delay:.0f}s "
        f"(rate_limit_retry #{job.rate_limit_retries + 1})",
    )


def _crash_decision(
    job: Job,
    config: SupervisorConfig,
    max_retries: int,
) -> RetryDecision:
    if job.retry_count >= max_retries:
        return RetryDecision(
            False,
            None,
            f"exceeded max retries ({max_retries})",
        )
    delay = min(
        config.backoff_base_seconds * (2 ** job.retry_count),
        _MAX_BACKOFF_SECONDS,
    )
    return RetryDecision(
        True,
        delay,
        f"transient error: retry #{job.retry_count + 1} in {delay:.0f}s",
    )
