import threading
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


def test_list_recent_done_filters_sorts_and_limits(tmp_path: Path):
    s = JobStore(tmp_path / "t.db")
    times = [
        "2026-08-20 00:00:00",
        "2026-08-20 00:00:00",
        "2026-08-20 00:00:01",
        "2026-08-20 00:00:01",
        "2026-08-20 00:00:02",
    ]
    ids = []
    for i, t in enumerate(times):
        jid = s.add(f"m{i}", f"{i}.pdf", f"inbox/{i}.pdf", "pdf", 100 + i, 1 + i)
        s.mark_printed(jid)
        s.conn.execute(
            "UPDATE print_jobs SET printed_at = ? WHERE id = ?",
            (t, jid),
        )
        s.conn.commit()
        ids.append(jid)

    pending = s.add("m_p", "p.pdf", "inbox/p.pdf", "pdf", 1, 1)
    failed = s.add("m_f", "f.pdf", "inbox/f.pdf", "pdf", 1, 1)
    s.set_status(failed, JobStatus.FAILED)
    discarded = s.add("m_d", "d.pdf", "inbox/d.pdf", "pdf", 1, 1)
    s.set_status(discarded, JobStatus.DISCARDED)

    recent = s.list_recent_done(3)
    assert [j.id for j in recent] == [ids[4], ids[3], ids[2]]
    assert all(j.status == JobStatus.DONE for j in recent)

    all_done = s.list_recent_done()
    assert [j.id for j in all_done] == [ids[4], ids[3], ids[2], ids[1], ids[0]]
    done_ids = {j.id for j in all_done}
    assert pending not in done_ids
    assert failed not in done_ids
    assert discarded not in done_ids


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


def test_mark_printed(tmp_path: Path):
    s = JobStore(tmp_path / "t.db")
    jid = s.add("m1", "a.pdf", "inbox/a.pdf", "pdf", 100, 1)
    s.mark_printed(jid)
    job = s.get(jid)
    assert job.status == JobStatus.DONE
    assert job.printed_at


def test_set_page_count(tmp_path: Path):
    s = JobStore(tmp_path / "t.db")
    jid = s.add("m1", "a.pdf", "inbox/a.pdf", "pdf", 100, 0)
    s.set_page_count(jid, 5)
    assert s.get(jid).page_count == 5


def test_on_job_added_callback(tmp_path: Path):
    s = JobStore(tmp_path / "t.db")
    seen = []
    s.on_job_added = seen.append
    jid = s.add("m1", "a.pdf", "inbox/a.pdf", "pdf", 100, 1)
    assert seen == [jid]


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


def test_can_use_jobstore_from_child_thread(tmp_path):
    s = JobStore(tmp_path / "t.db")
    jid = s.add("m1", "a.pdf", "inbox/a.pdf", "pdf", 100, 1)
    s.mark_printed(jid)

    errors = []

    def run_queries():
        try:
            assert len(s.list_active()) == 0
            assert len(s.list_recent_done()) == 1
        except Exception as exc:
            errors.append(exc)

    t = threading.Thread(target=run_queries)
    t.start()
    t.join()
    assert errors == []
