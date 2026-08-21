import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

import pymupdf as fitz
import pytest

from printshop.print_engine import PrintOptions
from printshop.xps_print import (
    A4_XPS_H,
    A4_XPS_W,
    build_xps,
    parse_pages,
    print_ticket_xml,
    submit_xps,
)

A4_PT = (595.0, 842.0)


def make_pdf(path: Path, size=A4_PT, pages: int = 1) -> Path:
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=size[0], height=size[1])
        page.insert_text((72, 72), "test")
    doc.save(str(path))
    doc.close()
    return path


def xps_names(xps: Path) -> set[str]:
    with zipfile.ZipFile(xps) as z:
        return set(z.namelist())


def read_entry(xps: Path, name: str) -> bytes:
    with zipfile.ZipFile(xps) as z:
        return z.read(name)


def test_parse_pages():
    assert parse_pages("1-3,5") == [1, 2, 3, 5]
    assert parse_pages("2") == [2]
    assert parse_pages("5,1") == [5, 1]
    assert parse_pages("") is None
    assert parse_pages("   ") is None


def test_parse_pages_deduplicates():
    assert parse_pages("1-3,3") == [1, 2, 3]
    assert parse_pages("5,5,1") == [5, 1]


@pytest.mark.parametrize("spec", ["abc", "1-x", "5-2", "0", "-1", "1,", ",1", ",,"])
def test_parse_pages_invalid_raises(spec):
    with pytest.raises(ValueError):
        parse_pages(spec)


def test_build_xps_valid_opc_package(tmp_path):
    pdf = make_pdf(tmp_path / "doc.pdf", pages=3)
    out = build_xps(pdf, tmp_path / "doc.xps", dpi=300)
    assert out.exists()
    with zipfile.ZipFile(out) as z:
        assert z.testzip() is None
    names = xps_names(out)
    assert "[Content_Types].xml" in names
    assert "_rels/.rels" in names
    assert "FixedDocumentSequence.fdseq" in names
    assert "FixedDocument.fdoc" in names
    assert "Pages/page1.fpage" in names
    assert "Pages/page3.fpage" in names
    assert "Pages/_rels/page1.fpage.rels" in names
    assert "Images/p1.png" in names
    assert "Images/p3.png" in names
    fdseq = read_entry(out, "FixedDocumentSequence.fdseq").decode()
    assert "DocumentReference" in fdseq


def test_build_xps_page_count(tmp_path):
    pdf = make_pdf(tmp_path / "doc.pdf", pages=3)
    out = build_xps(pdf, tmp_path / "doc.xps", dpi=300)
    fdoc = read_entry(out, "FixedDocument.fdoc").decode()
    assert fdoc.count("PageContent") == 3


def test_build_xps_page_selection(tmp_path):
    pdf = make_pdf(tmp_path / "doc.pdf", pages=5)
    out = build_xps(pdf, tmp_path / "sel.xps", dpi=300, pages="1-3,5")
    fdoc = read_entry(out, "FixedDocument.fdoc").decode()
    assert fdoc.count("PageContent") == 4
    names = xps_names(out)
    assert "Images/p1.png" in names
    assert "Images/p4.png" in names


def test_build_xps_all_pages_when_empty(tmp_path):
    pdf = make_pdf(tmp_path / "doc.pdf", pages=5)
    out = build_xps(pdf, tmp_path / "all.xps", dpi=300)
    fdoc = read_entry(out, "FixedDocument.fdoc").decode()
    assert fdoc.count("PageContent") == 5


def test_build_xps_empty_selection_raises(tmp_path):
    pdf = make_pdf(tmp_path / "doc.pdf", pages=3)
    with pytest.raises(ValueError, match="页码范围为空"):
        build_xps(pdf, tmp_path / "sel.xps", dpi=300, pages="9")


def test_build_xps_max_pages_raises(tmp_path):
    pdf = make_pdf(tmp_path / "doc.pdf", pages=5)
    with pytest.raises(ValueError, match="上限"):
        build_xps(pdf, tmp_path / "sel.xps", dpi=300, max_pages=3)


def test_build_xps_cleans_intermediate_pngs(tmp_path):
    pdf = make_pdf(tmp_path / "doc.pdf", pages=3)
    out = build_xps(pdf, tmp_path / "doc.xps", dpi=300)
    work_dir = out.parent
    assert list(work_dir.glob("p*.png")) == []


def test_build_xps_viewbox_natural_units(tmp_path):
    pdf = make_pdf(tmp_path / "a4.pdf")
    out = build_xps(pdf, tmp_path / "a4.xps", dpi=300)
    fpage = read_entry(out, "Pages/page1.fpage").decode()
    m = re.search(r'Viewbox="0,0,([\d.]+),([\d.]+)"', fpage)
    vw, vh = float(m.group(1)), float(m.group(2))
    assert abs(vw - A4_XPS_W) < 5
    assert abs(vh - A4_XPS_H) < 5


def test_build_xps_contain_fit_centered(tmp_path):
    pdf = make_pdf(tmp_path / "narrow.pdf", size=(300, 900))
    out = build_xps(pdf, tmp_path / "narrow.xps", dpi=300)
    fpage = read_entry(out, "Pages/page1.fpage").decode()
    m = re.search(r'Viewport="([\d.]+),([\d.]+),([\d.]+),([\d.]+)"', fpage)
    x, y, w, h = (float(m.group(i)) for i in range(1, 5))
    assert w <= A4_XPS_W + 0.5 and h <= A4_XPS_H + 0.5
    assert abs(x - (A4_XPS_W - w) / 2) < 1
    assert abs(y - (A4_XPS_H - h) / 2) < 1


def test_print_ticket_xml_duplex_color_copies_paper():
    o = PrintOptions(printer="P", color=False, copies=3,
                     duplex="duplexlong", paper="A4")
    xml = print_ticket_xml(o)
    assert "TwoSidedLongEdge" in xml
    assert "Monochrome" in xml
    assert "JobCopiesAllDocuments" in xml
    assert ">3</psf:Value>" in xml
    assert "ISOA4" in xml


def test_print_ticket_xml_simplex_color_default():
    o = PrintOptions(printer="P")
    xml = print_ticket_xml(o)
    assert "OneSided" in xml
    assert "Color" in xml


def test_print_ticket_xml_duplex_short():
    o = PrintOptions(printer="P", duplex="duplexshort")
    xml = print_ticket_xml(o)
    assert "TwoSidedShortEdge" in xml
    assert "TwoSidedLongEdge" not in xml
    assert "OneSided" not in xml


def test_print_ticket_xml_non_a4_raises():
    with pytest.raises(ValueError, match="A4"):
        print_ticket_xml(PrintOptions(printer="P", paper="Letter"))
    with pytest.raises(ValueError, match="A4"):
        print_ticket_xml(PrintOptions(printer="P", paper=""))
    xml = print_ticket_xml(PrintOptions(printer="P", paper="a4"))
    assert "ISOA4" in xml


def test_submit_xps_ok(tmp_path, monkeypatch):
    xps = tmp_path / "a.xps"
    xps.write_bytes(b"x")

    class R:
        returncode = 0
        stdout = b"OK"
        stderr = b""

    calls = {}

    def fake_run(argv, capture_output=True, timeout=300):
        calls["argv"] = argv
        return R()

    monkeypatch.setattr("printshop.xps_print.subprocess.run", fake_run)
    ok, err = submit_xps(xps, PrintOptions(printer="P"))
    assert ok and err == ""
    assert any("submit_xps.ps1" in a for a in calls["argv"])
    assert "P" in calls["argv"]


def test_submit_xps_printer_missing(tmp_path, monkeypatch):
    xps = tmp_path / "a.xps"
    xps.write_bytes(b"x")

    class R:
        returncode = 4
        stdout = b""
        stderr = b""

    monkeypatch.setattr("printshop.xps_print.subprocess.run", lambda *a, **k: R())
    ok, err = submit_xps(xps, PrintOptions(printer="missing"))
    assert not ok and err == "打印机不存在"


def test_submit_xps_unknown_code_uses_stderr(tmp_path, monkeypatch):
    xps = tmp_path / "a.xps"
    xps.write_bytes(b"x")

    class R:
        returncode = 1
        stdout = b""
        stderr = b"boom detail"

    monkeypatch.setattr("printshop.xps_print.subprocess.run", lambda *a, **k: R())
    ok, err = submit_xps(xps, PrintOptions(printer="P"))
    assert not ok and "boom detail" in err


def test_submit_xps_ps1_missing(tmp_path, monkeypatch):
    xps = tmp_path / "a.xps"
    xps.write_bytes(b"x")
    monkeypatch.setattr("printshop.xps_print._PS1_NAME", "no_such.ps1")
    ok, err = submit_xps(xps, PrintOptions(printer="P"))
    assert not ok and "缺少提交助手脚本" in err


def test_submit_xps_timeout_expired(tmp_path, monkeypatch):
    xps = tmp_path / "a.xps"
    xps.write_bytes(b"x")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(a[0], 300)

    monkeypatch.setattr("printshop.xps_print.subprocess.run", boom)
    ok, err = submit_xps(xps, PrintOptions(printer="P"))
    assert not ok and "提交执行失败" in err


def test_submit_xps_cleans_ticket_on_exception(tmp_path, monkeypatch):
    xps = tmp_path / "a.xps"
    xps.write_bytes(b"x")
    created = {}

    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(a[0], 300)

    real = tempfile.NamedTemporaryFile

    class Tracker:
        def __init__(self, *a, **k):
            self._inner = real(*a, **k)
            created["name"] = self._inner.name

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

        def write(self, data):
            self._inner.write(data)

        @property
        def name(self):
            return self._inner.name

    monkeypatch.setattr("printshop.xps_print.tempfile.NamedTemporaryFile", Tracker)
    monkeypatch.setattr("printshop.xps_print.subprocess.run", fake_run)
    submit_xps(xps, PrintOptions(printer="P"))
    assert "name" in created
    assert not Path(created["name"]).exists()


def test_submit_xps_ps1_has_utf8_output_encoding():
    ps1 = Path(__file__).resolve().parents[1] / "submit_xps.ps1"
    text = ps1.read_text(encoding="utf-8-sig")
    assert "OutputEncoding" in text and "UTF8" in text
