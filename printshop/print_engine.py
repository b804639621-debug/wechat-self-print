import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pymupdf as fitz
from PIL import Image

from printshop.config import Config
from printshop.xps_print import build_xps, submit_xps

A4_WIDTH = 595.0
A4_HEIGHT = 842.0

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


def _unique_out(work_dir: Path, src: Path) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir / f"{src.stem}-{uuid4().hex[:8]}.pdf"


def _convert_with_office_com(src: Path, out: Path) -> bool:
    """Office COM（win32com, ExportAsFixedFormat）转 PDF。可用且成功返回 True。"""
    try:
        import win32com.client
    except ImportError:
        return False
    try:
        ext = src.suffix.lower()
        if ext in (".doc", ".docx"):
            app = win32com.client.DispatchEx("Word.Application")
            try:
                app.Visible = False
                doc = app.Documents.Open(str(src), ReadOnly=True)
                try:
                    doc.ExportAsFixedFormat(str(out), 17)  # wdExportFormatPDF
                finally:
                    doc.Close(False)
            finally:
                app.Quit()
        elif ext in (".xls", ".xlsx"):
            app = win32com.client.DispatchEx("Excel.Application")
            try:
                app.Visible = False
                wb = app.Workbooks.Open(str(src), ReadOnly=True)
                try:
                    wb.ExportAsFixedFormat(0, str(out))  # xlTypePDF
                finally:
                    wb.Close(False)
            finally:
                app.Quit()
        elif ext in (".ppt", ".pptx"):
            app = win32com.client.DispatchEx("PowerPoint.Application")
            try:
                prs = app.Presentations.Open(str(src), ReadOnly=True)
                try:
                    prs.SaveAs(str(out), 32)  # ppSaveAsPDF
                finally:
                    prs.Close()
            finally:
                app.Quit()
        else:
            return False
    except Exception:
        return False
    return out.exists()


def _convert_with_soffice(src: Path, out: Path, cfg: Config) -> bool:
    """LibreOffice headless 转 PDF。soffice 不可用返回 False，转换异常抛 RuntimeError。"""
    soffice = Path(cfg.soffice_path)
    if not soffice.exists():
        return False
    with tempfile.TemporaryDirectory(dir=str(out.parent)) as td:
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
    return True


def normalize_to_pdf(src: Path, work_dir: Path, cfg: Config) -> Path:
    """把源文件归一为 PDF。

    Office 双轨制：Office COM（ExportAsFixedFormat）优先 → LibreOffice 兜底 →
    均不可用抛 FileNotFoundError（界面标红）。图片转 PDF（白底去透明通道）。
    返回的 PDF 不再做 A4 归一化——尺寸适配统一由 XPS 层 Viewport/Viewbox 完成。
    """
    src = Path(src)
    work_dir = Path(work_dir)
    kind = classify_file(src)
    if kind == "pdf":
        out = _unique_out(work_dir, src)
        shutil.copy(src, out)
    elif kind == "image":
        out = _unique_out(work_dir, src)
        img = Image.open(src).convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])  # 白底去透明通道
        bg.save(out, "PDF")
    elif kind == "office":
        out = _unique_out(work_dir, src)
        if not (_convert_with_office_com(src, out) or _convert_with_soffice(src, out, cfg)):
            raise FileNotFoundError("未安装 Office 或 LibreOffice，无法转换 Office 文件")
    else:
        raise ValueError(f"不支持的文件类型: {src.name}")
    return out


def get_page_count(pdf: Path) -> int:
    with fitz.open(str(pdf)) as doc:
        return doc.page_count


def render_page_png(pdf: Path, page: int, dpi: int = 110) -> bytes:
    with fitz.open(str(pdf)) as doc:
        return doc[page].get_pixmap(dpi=dpi).tobytes("png")


def print_pdf(cfg: Config, pdf: Path, o: PrintOptions) -> tuple[bool, str]:
    """XPS 路径打印：渲染打包（render_dpi/pages）→ PrintTicket 提交。

    对外契约不变：签名与返回语义与 SumatraPDF 时代一致，routes.py 无需改动。
    """
    work = Path(cfg.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    xps_path = work / f"{Path(pdf).stem}-{uuid4().hex[:8]}.xps"
    build_xps(pdf, xps_path, cfg.render_dpi, o.pages)
    return submit_xps(xps_path, o)
