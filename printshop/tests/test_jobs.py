from pathlib import Path
from printshop.jobs import JobStore, JobStatus


def test_add_list_archive(tmp_path: Path):
    s = JobStore(tmp_path / "t.db")
    jid = s.add("m1", "a.pdf", "inbox/a.pdf", "pdf", 100, 3)
    assert jid >= 1
    jobs = s.list_active()
    assert len(jobs) == 1 and jobs[0].file_name == "a.pdf" and jobs[0].status == JobStatus.PENDING
    s.set_status(jid, JobStatus.DONE, options_json='{"printer":"P1"}')
    assert s.get(jid).status == JobStatus.DONE
    assert s.archive_all() == 1
    assert s.list_active() == []


def test_list_active_filters_done_and_discarded(tmp_path: Path):
    s = JobStore(tmp_path / "t.db")
    a = s.add("m1", "a.pdf", "inbox/a.pdf", "pdf", 100, 1)
    b = s.add("m2", "b.pdf", "inbox/b.pdf", "pdf", 200, 2)
    c = s.add("m3", "c.pdf", "inbox/c.pdf", "pdf", 300, 3)
    d = s.add("m4", "d.pdf", "inbox/d.pdf", "pdf", 400, 4)
    s.set_status(a, JobStatus.DONE)
    s.set_status(b, JobStatus.FAILED)
    s.set_status(c, JobStatus.DISCARDED)
    assert s.get(d).status == JobStatus.PENDING
    active = {j.id for j in s.list_active()}
    assert active == {b, d}


def test_get_missing_returns_none(tmp_path: Path):
    s = JobStore(tmp_path / "t.db")
    assert s.get(999) is None


def test_set_status_error(tmp_path: Path):
    s = JobStore(tmp_path / "t.db")
    jid = s.add("m1", "a.pdf", "inbox/a.pdf", "pdf", 100, 1)
    s.set_status(jid, JobStatus.FAILED, error="no printer")
    job = s.get(jid)
    assert job.status == JobStatus.FAILED
    assert job.error == "no printer"
    assert job.options_json == ""


def test_persists_across_reopen(tmp_path: Path):
    db = tmp_path / "t.db"
    s = JobStore(db)
    jid = s.add("m1", "a.pdf", "inbox/a.pdf", "pdf", 100, 1)
    s.set_status(jid, JobStatus.PRINTING)
    s = JobStore(db)
    jobs = s.list_active()
    assert len(jobs) == 1 and jobs[0].status == JobStatus.PRINTING
    assert s.archive_all() == 1
    s = JobStore(db)
    assert s.list_active() == []
