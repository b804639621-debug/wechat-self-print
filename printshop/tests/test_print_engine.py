import shutil
import subprocess
from pathlib import Path

import pymupdf as fitz
import pytest
import win32print
from PIL import Image

from printshop.config import Config
from printshop.print_engine import (
    PrintOptions,
    classify_file,
    get_page_count,
    list_printers,
    normalize_to_pdf,
    print_pdf,
    render_page_png,
)

A4_W, A4_H = 595.0, 842.0


def make_pdf(path: Path, size=(A4_W, A4_H), pages: int = 1) -> Path:
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=size[0], height=size[1])
        page.insert_text((72, 72), "test")
    doc.save(str(path))
    doc.close()
    return path


def test_classify():
    assert classify_file(Path("a.pdf")) == "pdf"
    assert classify_file(Path("a.JPG")) == "image"
    assert classify_file(Path("a.docx")) == "office"
    assert classify_file(Path("a.zip")) == "other"


def test_list_printers_returns_names(monkeypatch):
    fake = lambda *a, **k: [
        (0, "x", "HP 彩机", "LPT1", "hpd"),
        (0, "y", "EPSON L6370", "USB", "ep"),
    ]
    monkeypatch.setattr(win32print, "EnumPrinters", fake)
    assert list_printers() == ["HP 彩机", "EPSON L6370"]


def test_list_printers_empty_on_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("no printers")
    monkeypatch.setattr(win32print, "EnumPrinters", boom)
    assert list_printers() == []


def test_normalize_to_pdf_pdf_direct(tmp_path):
    cfg = Config()
    src = make_pdf(tmp_path / "doc.pdf", (612, 792))
    out = normalize_to_pdf(src, tmp_path / "work", cfg)
    assert out.exists() and out.suffix == ".pdf"
    assert out != src


def test_normalize_to_pdf_image(tmp_path):
    cfg = Config()
    src = tmp_path / "pic.JPG"
    Image.new("RGB", (500, 700), "white").save(src, "JPEG")
    out = normalize_to_pdf(src, tmp_path / "work", cfg)
    assert out.exists() and out.suffix == ".pdf"


def test_normalize_to_pdf_other_raises(tmp_path):
    cfg = Config()
    src = tmp_path / "a.zip"
    src.write_bytes(b"PK")
    with pytest.raises(ValueError):
        normalize_to_pdf(src, tmp_path / "work", cfg)


def test_normalize_to_pdf_office_uses_office_com_first(tmp_path, monkeypatch):
    cfg = Config()
    src = tmp_path / "doc.docx"
    src.write_bytes(b"fake docx")
    pdf = make_pdf(tmp_path / "com_out.pdf")

    def fake_com(s, out):
        shutil.copy(pdf, out)
        return True

    def fake_soffice(*a, **k):
        raise AssertionError("soffice should not be called")

    monkeypatch.setattr("printshop.print_engine._convert_with_office_com", fake_com)
    monkeypatch.setattr("printshop.print_engine._convert_with_soffice", fake_soffice)
    out = normalize_to_pdf(src, tmp_path / "work", cfg)
    assert out.exists() and out.suffix == ".pdf"


def test_normalize_to_pdf_office_falls_back_to_soffice(tmp_path, monkeypatch):
    cfg = Config()
    cfg.soffice_path = str(tmp_path / "soffice.exe")
    (tmp_path / "soffice.exe").write_bytes(b"")
    src = tmp_path / "doc.docx"
    src.write_bytes(b"fake docx")
    letter = make_pdf(tmp_path / "letter.pdf", (612, 792))

    def fake_run(argv, capture_output=True, timeout=180):
        outdir = Path(argv[argv.index("--outdir") + 1])
        shutil.copy(letter, outdir / (Path(argv[-1]).stem + ".pdf"))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("printshop.print_engine._convert_with_office_com",
                        lambda s, o: False)
    monkeypatch.setattr("printshop.print_engine.subprocess.run", fake_run)
    out = normalize_to_pdf(src, tmp_path / "work", cfg)
    assert out.exists() and out.suffix == ".pdf"


def test_normalize_to_pdf_office_unavailable_raises(tmp_path, monkeypatch):
    cfg = Config()
    cfg.soffice_path = str(tmp_path / "nope" / "soffice.exe")
    src = tmp_path / "doc.docx"
    src.write_bytes(b"fake docx")
    monkeypatch.setattr("printshop.print_engine._convert_with_office_com",
                        lambda s, o: False)
    monkeypatch.setattr("printshop.print_engine._convert_with_soffice",
                        lambda s, o, c: False)
    with pytest.raises(FileNotFoundError):
        normalize_to_pdf(src, tmp_path / "work", cfg)


def test_get_page_count(tmp_path):
    pdf = make_pdf(tmp_path / "two.pdf", pages=2)
    assert get_page_count(pdf) == 2


def test_render_page_png(tmp_path):
    pdf = make_pdf(tmp_path / "one.pdf")
    data = render_page_png(pdf, 0)
    assert data.startswith(b"\x89PNG")


def test_print_pdf_success(tmp_path, monkeypatch):
    cfg = Config()
    pdf = make_pdf(tmp_path / "p.pdf")

    def fake_build(pdf, out, dpi, pages):
        out.write_bytes(b"xps")
        return out

    monkeypatch.setattr("printshop.print_engine.build_xps", fake_build)
    monkeypatch.setattr("printshop.print_engine.submit_xps",
                        lambda *a, **k: (True, ""))
    ok, err = print_pdf(cfg, pdf, PrintOptions(printer="P"))
    assert ok and err == ""


def test_print_pdf_error_code(tmp_path, monkeypatch):
    cfg = Config()
    pdf = make_pdf(tmp_path / "p.pdf")
    monkeypatch.setattr("printshop.print_engine.build_xps",
                        lambda *a, **k: Path("out.xps"))
    monkeypatch.setattr("printshop.print_engine.submit_xps",
                        lambda *a, **k: (False, "打印机不存在"))
    ok, err = print_pdf(cfg, pdf, PrintOptions(printer="missing"))
    assert not ok and err == "打印机不存在"


def test_print_pdf_passes_render_dpi_and_pages(tmp_path, monkeypatch):
    cfg = Config()
    cfg.render_dpi = 600
    pdf = make_pdf(tmp_path / "p.pdf")
    seen = {}

    def fake_build(pdf, out, dpi, pages):
        seen["dpi"] = dpi
        seen["pages"] = pages
        out.write_bytes(b"xps")
        return out

    monkeypatch.setattr("printshop.print_engine.build_xps", fake_build)
    monkeypatch.setattr("printshop.print_engine.submit_xps",
                        lambda *a, **k: (True, ""))
    ok, err = print_pdf(cfg, pdf, PrintOptions(printer="P", pages="1-3,5"))
    assert ok and err == ""
    assert seen["dpi"] == 600 and seen["pages"] == "1-3,5"
