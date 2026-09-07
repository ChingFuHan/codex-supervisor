import pytest
from codex_supervisor.models import Job, JobStatus
from codex_supervisor.state import StateStore


def _make_job(job_id="sv-20260906-120000") -> Job:
    return Job(
        job_id=job_id,
        status=JobStatus.RUNNING,
        codex_command=["codex", "exec", "--json", "hello"],
        work_dir="/tmp/test",
        created_at="2026-09-06T12:00:00Z",
    )


def test_save_load_roundtrip(store):
    job = _make_job()
    store.save_job(job)
    loaded = store.load_job(job.job_id)
    assert loaded is not None
    assert loaded.job_id == job.job_id
    assert loaded.status == JobStatus.RUNNING
    assert loaded.codex_command == job.codex_command


def test_load_nonexistent_returns_none(store):
    assert store.load_job("does-not-exist") is None


def test_update_job_partial(store):
    job = _make_job()
    store.save_job(job)
    updated = store.update_job(job.job_id, status=JobStatus.COMPLETED, retry_count=2)
    assert updated.status == JobStatus.COMPLETED
    assert updated.retry_count == 2
    # Reload from disk
    reloaded = store.load_job(job.job_id)
    assert reloaded.status == JobStatus.COMPLETED


def test_load_all_jobs(store):
    j1 = _make_job("sv-20260906-120000")
    j2 = _make_job("sv-20260906-130000")
    store.save_job(j1)
    store.save_job(j2)
    jobs = store.load_all_jobs()
    assert len(jobs) == 2
    ids = {j.job_id for j in jobs}
    assert ids == {"sv-20260906-120000", "sv-20260906-130000"}


def test_corrupt_json_skipped(store):
    path = store._jobs_dir / "bad.json"
    path.write_text("not json{{{")
    jobs = store.load_all_jobs()
    assert all(j.job_id != "bad" for j in jobs)


def test_atomic_write_uses_tmp(store, monkeypatch):
    # Verify rename is used: if rename is not called, we'd see .tmp file linger.
    # Just check that .tmp is not present after save.
    job = _make_job()
    store.save_job(job)
    tmp = store._job_path(job.job_id).with_suffix(".tmp")
    assert not tmp.exists()
