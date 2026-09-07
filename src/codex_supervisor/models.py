import dataclasses
import datetime
import enum
from pathlib import Path


class ExitClassification(enum.Enum):
    NORMAL_COMPLETION = "normal_completion"
    RATE_LIMIT = "rate_limit"
    TRANSIENT_ERROR = "transient_error"
    USER_INTERRUPT = "user_interrupt"
    UNKNOWN_FAILURE = "unknown_failure"


class JobStatus(enum.Enum):
    RUNNING = "running"
    RATE_LIMITED = "rate_limited"
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclasses.dataclass
class RateLimitEvent:
    detected: bool
    reset_at: datetime.datetime | None
    raw_message: str
    confidence: float  # 0.0 - 1.0
    source: str  # "jsonl_event", "stderr", "goals_db", "exit_code"


@dataclasses.dataclass
class RetryDecision:
    should_retry: bool
    delay_seconds: float | None
    reason: str


@dataclasses.dataclass
class Job:
    job_id: str
    status: JobStatus
    codex_command: list[str]
    work_dir: str
    created_at: str
    session_id: str | None = None
    last_start: str | None = None
    last_exit: str | None = None
    rate_limit_detected: str | None = None
    parsed_reset: str | None = None
    scheduled_resume: str | None = None
    retry_count: int = 0
    rate_limit_retries: int = 0
    last_error: str | None = None
    exit_classification: str | None = None
    resume_prompt: str = "continue"
    mode: str = "exec"
    observed_event: str | None = None
    submitted_event: str | None = None
    queued_at: str | None = None
    queue_id: str | None = None

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        d = dict(d)
        d["status"] = JobStatus(d["status"])
        return cls(**d)


@dataclasses.dataclass
class SupervisorConfig:
    state_dir: Path
    fallback_wait_minutes: int = 30
    max_crash_retries: int = 5
    max_unknown_retries: int = 3
    backoff_base_seconds: int = 30
    codex_path: str | None = None  # None = auto-detect via shutil.which
    max_wait_seconds: int = 4 * 3600

    def __post_init__(self):
        for name in ("fallback_wait_minutes", "backoff_base_seconds", "max_wait_seconds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("max_crash_retries", "max_unknown_retries"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")
