import sys
from pathlib import Path

import pytest

# Make src/ importable without pip install
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from codex_supervisor.config import load_config
from codex_supervisor.models import SupervisorConfig
from codex_supervisor.state import StateStore


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "state"


@pytest.fixture
def store(state_dir):
    return StateStore(state_dir)


@pytest.fixture
def config(state_dir):
    return SupervisorConfig(
        state_dir=state_dir,
        fallback_wait_minutes=30,
        max_crash_retries=5,
        max_unknown_retries=3,
        backoff_base_seconds=30,
    )


@pytest.fixture
def fake_codex_dir():
    return Path(__file__).parent / "fake_codex"
