import os
from pathlib import Path
from .models import SupervisorConfig


def _xdg_state_home() -> Path:
    val = os.environ.get("XDG_STATE_HOME", "")
    return Path(val) if val else Path.home() / ".local" / "state"


def load_config() -> SupervisorConfig:
    state_dir = Path(
        os.environ.get(
            "CODEX_SUPERVISOR_STATE_DIR",
            str(_xdg_state_home() / "codex-supervisor"),
        )
    )
    return SupervisorConfig(
        state_dir=state_dir,
        fallback_wait_minutes=int(
            os.environ.get("CODEX_SUPERVISOR_FALLBACK_WAIT_MINUTES", "30")
        ),
        max_crash_retries=int(
            os.environ.get("CODEX_SUPERVISOR_MAX_CRASH_RETRIES", "5")
        ),
        max_unknown_retries=int(
            os.environ.get("CODEX_SUPERVISOR_MAX_UNKNOWN_RETRIES", "3")
        ),
        backoff_base_seconds=int(
            os.environ.get("CODEX_SUPERVISOR_BACKOFF_BASE_SECONDS", "30")
        ),
        codex_path=os.environ.get("CODEX_SUPERVISOR_CODEX_PATH"),
        max_wait_seconds=int(os.environ.get("CODEX_SUPERVISOR_MAX_WAIT_SECONDS", "14400")),
    )
