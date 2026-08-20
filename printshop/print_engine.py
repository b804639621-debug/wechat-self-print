import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pymupdf as fitz
from PIL import Image

from printshop.config import Config

EXIT_CODES = {0: "", 2: "文件无法打开或格式不支持", 3: "文档禁止打印",
              4: "打印机不存在", 5: "打印机驱动/设备失败", 6: "打印被策略禁止"}

A4_WIDTH = 595.0
A4_HEIGHT = 842.0
A4_TOLERANCE = 2.0

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
_OFFICE_EXTS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp"}


@dataclass
class PrintOptions:
    printer: str
    color: bool = True          # False=灰度
    copies: int = 1
    pages: str = ""             # ""=全部，或 "1-3,5"
    duplex: str = "simplex"     # simplex|duplexlong|duplexshort
    paper: str = "A4"


def list_printers() -> list[str]:
    try:
        import win32print
    except ImportError:
        return []
    try:
        printers = win32print.EnumPrinters(
            win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)
    except Exception:
        return []
    return [p[2] for p in printers]


def classify_file(path: Path) -> str:
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _OFFICE_EXTS:
        return "office"
    return "other"


def _is_a4(rect) -> bool:
    w, h = rect.width, rect.height
    return (abs(w - A4_WIDTH) <= A4_TOLERANCE and abs(h - A4_HEIGHT) <= A4_TOLERANCE) or \
           (abs(w - A4_HEIGHT) <= A4_TOLERANCE and abs(h - A4_WIDTH) <= A4_TOLERANCE)


def _fit_rect(rect, target: fitz.Rect) -> fitz.Rect:
    scale = min(target.width / rect.width, target.height / rect.height)
    w = rect.width * scale
    h = rect.height * scale
    x0 = (target.width - w) / 2
    y0 = (target.height - h) / 2
    return fitz.Rect(x0, y0, x0 + w, y0 + h)


def normalize_page_size_to_a4(pdf: Path) -> Path:
    """R3：逐页检查，非 A4 页面缩放嵌入 A4 页面（保持比例、居中）。已是 A4 则原样返回。"""
    pdf = Path(pdf)
    with fitz.open(str(pdf)) as doc:
        if all(_is_a4(p.rect) for p in doc):
            return pdf
        a4 = fitz.Rect(0, 0, A4_WIDTH, A4_HEIGHT)
        out = fitz.open()
        try:
            for page in doc:
                if _is_a4(page.rect):
                    out.insert_pdf(doc, from_page=page.number, to_page=page.number)
                else:
                    new_page = out.new_page(width=A4_WIDTH, height=A4_HEIGHT)
                    new_page.show_pdf_page(_fit_rect(page.rect, a4), doc, page.number)
            out_path = pdf.with_name(f"{pdf.stem}_a4.pdf")
            out.save(str(out_path), garbage=3, deflate=True)
        finally:
            out.close()
        return out_path


def _unique_out(work_dir: Path, src: Path) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir / f"{src.stem}-{uuid4().hex[:8]}.pdf"


def normalize_to_pdf(src: Path, work_dir: Path, cfg: Config) -> Path:
    src = Path(src)
    work_dir = Path(work_dir)
    kind = classify_file(src)
    if kind == "pdf":
        out = _unique_out(work_dir, src)
        shutil.copy(src, out)
    elif kind == "image":
        out = _unique_out(work_dir, src)
        Image.open(src).convert("RGB").save(out, "PDF")
    elif kind == "office":
        soffice = Path(cfg.soffice_path)
        if not soffice.exists():
            raise FileNotFoundError("未安装 LibreOffice")
        out = _unique_out(work_dir, src)
        with tempfile.TemporaryDirectory(dir=str(work_dir)) as td:
            r = subprocess.run(
                [str(soffice), "--headless", "--convert-to", "pdf",
                 "--outdir", td, str(src)],
                capture_output=True, timeout=180)
            if r.returncode != 0:
                raise RuntimeError(f"LibreOffice 转换失败，退出码 {r.returncode}")
            produced = Path(td) / (src.stem + ".pdf")
            if not produced.exists():
                raise RuntimeError(f"LibreOffice 未生成输出文件: {src.name}")
            shutil.copy(produced, out)
    else:
        raise ValueError(f"不支持的文件类型: {src.name}")
    return normalize_page_size_to_a4(out)


def get_page_count(pdf: Path) -> int:
    with fitz.open(str(pdf)) as doc:
        return doc.page_count


def render_page_png(pdf: Path, page: int, dpi: int = 110) -> bytes:
    with fitz.open(str(pdf)) as doc:
        return doc[page].get_pixmap(dpi=dpi).tobytes("png")


def build_print_cmd(cfg: Config, pdf: Path, o: PrintOptions) -> list[str]:
    # 注：EPSON L6370 驱动会丢弃 SumatraPDF 下发的 duplex/paper 设置（M1 §7 已知未决，用户排查中），照常下发。
    settings = ["color" if o.color else "monochrome", f"{o.copies}x",
                o.duplex, f"paper={o.paper}", "fit"]
    if o.pages.strip():
        settings.insert(0, o.pages.strip())
    return [str(cfg.sumatra_path), "-print-to", o.printer,
            "-print-settings", ",".join(settings), "-silent", str(pdf)]


def print_pdf(cfg: Config, pdf: Path, o: PrintOptions) -> tuple[bool, str]:
    r = subprocess.run(build_print_cmd(cfg, pdf, o), capture_output=True, timeout=300)
    return (r.returncode == 0, EXIT_CODES.get(r.returncode, f"未知错误码 {r.returncode}"))
