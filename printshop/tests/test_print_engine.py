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
    build_print_cmd,
    classify_file,
    get_page_count,
    list_printers,
    normalize_page_size_to_a4,
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


def is_a4(pdf: Path) -> bool:
    with fitz.open(str(pdf)) as doc:
        return all(
            (abs(p.rect.width - A4_W) <= 2 and abs(p.rect.height - A4_H) <= 2)
            or (abs(p.rect.width - A4_H) <= 2 and abs(p.rect.height - A4_W) <= 2)
            for p in doc
        )


def test_classify():
    assert classify_file(Path("a.pdf")) == "pdf"
    assert classify_file(Path("a.JPG")) == "image"
    assert classify_file(Path("a.docx")) == "office"
    assert classify_file(Path("a.zip")) == "other"


def test_build_cmd_mono_duplex():
    cfg = Config()
    cmd = build_print_cmd(cfg, Path("x.pdf"),
                          PrintOptions(printer="HP 彩机", color=False, copies=2,
                                       pages="1-3", duplex="duplexlong", paper="A4"))
    s = " ".join(cmd)
    assert "-print-to" in cmd and "HP 彩机" in cmd
    assert "monochrome" in s and "2x" in s and "1-3" in s and "duplexlong" in s and "paper=A4" in s
    assert "-silent" in cmd


def test_build_cmd_color_simplex_allpages():
    cfg = Config()
    cmd = build_print_cmd(cfg, Path("x.pdf"), PrintOptions(printer="P"))
    s = " ".join(cmd)
    assert "color" in s and "monochrome" not in s and "simplex" in s


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


def test_normalize_page_size_to_a4_converts_letter(tmp_path):
    src = make_pdf(tmp_path / "letter.pdf", (612, 792))
    out = normalize_page_size_to_a4(src)
    assert out.exists() and is_a4(out)
    with fitz.open(str(out)) as doc:
        assert doc.page_count == 1


def test_normalize_page_size_to_a4_skips_when_already_a4(tmp_path):
    src = make_pdf(tmp_path / "a4.pdf", (A4_W, A4_H))
    assert normalize_page_size_to_a4(src) == src


def test_normalize_to_pdf_pdf_direct(tmp_path):
    cfg = Config()
    src = make_pdf(tmp_path / "doc.pdf", (612, 792))
    out = normalize_to_pdf(src, tmp_path / "work", cfg)
    assert out.exists() and out.suffix == ".pdf"
    assert is_a4(out)
    with fitz.open(str(out)) as doc:
        assert doc.page_count == 1


def test_normalize_to_pdf_image(tmp_path):
    cfg = Config()
    src = tmp_path / "pic.JPG"
    Image.new("RGB", (500, 700), "white").save(src, "JPEG")
    out = normalize_to_pdf(src, tmp_path / "work", cfg)
    assert out.exists() and out.suffix == ".pdf"
    assert is_a4(out)


def test_normalize_to_pdf_other_raises(tmp_path):
    cfg = Config()
    src = tmp_path / "a.zip"
    src.write_bytes(b"PK")
    with pytest.raises(ValueError):
        normalize_to_pdf(src, tmp_path / "work", cfg)


def test_normalize_to_pdf_office_missing_soffice(tmp_path):
    cfg = Config()
    cfg.soffice_path = str(tmp_path / "nope" / "soffice.exe")
    src = tmp_path / "doc.docx"
    src.write_bytes(b"fake docx")
    with pytest.raises(FileNotFoundError, match="LibreOffice"):
        normalize_to_pdf(src, tmp_path / "work", cfg)


def test_normalize_to_pdf_office(tmp_path, monkeypatch):
    cfg = Config()
    cfg.soffice_path = str(tmp_path / "soffice.exe")
    (tmp_path / "soffice.exe").write_bytes(b"")
    letter = make_pdf(tmp_path / "letter.pdf", (612, 792))
    src = tmp_path / "doc.docx"
    src.write_bytes(b"fake docx")

    def fake_run(argv, capture_output=True, timeout=180):
        outdir = Path(argv[argv.index("--outdir") + 1])
        shutil.copy(letter, outdir / (Path(argv[-1]).stem + ".pdf"))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("printshop.print_engine.subprocess.run", fake_run)
    out = normalize_to_pdf(src, tmp_path / "work", cfg)
    assert out.exists() and out.suffix == ".pdf"
    assert is_a4(out)


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

    class R:
        returncode = 0
    monkeypatch.setattr("printshop.print_engine.subprocess.run", lambda *a, **k: R())
    ok, err = print_pdf(cfg, pdf, PrintOptions(printer="P"))
    assert ok and err == ""


def test_print_pdf_error_code(tmp_path, monkeypatch):
    cfg = Config()
    pdf = make_pdf(tmp_path / "p.pdf")

    class R:
        returncode = 4
    monkeypatch.setattr("printshop.print_engine.subprocess.run", lambda *a, **k: R())
    ok, err = print_pdf(cfg, pdf, PrintOptions(printer="missing"))
    assert not ok and err == "打印机不存在"


def test_print_pdf_unknown_code(tmp_path, monkeypatch):
    cfg = Config()
    pdf = make_pdf(tmp_path / "p.pdf")

    class R:
        returncode = 99
    monkeypatch.setattr("printshop.print_engine.subprocess.run", lambda *a, **k: R())
    ok, err = print_pdf(cfg, pdf, PrintOptions(printer="P"))
    assert not ok and err.startswith("未知错误码")
