"""XPS 打包 + PrintTicket 提交（打印引擎 XPS 路径）。
以 tools/poc/file_to_xps.py 与 tools/poc/make_xps.py 为起点做工程化：
- 错误处理、类型标注、docstring；
- 保留 PoC 已实测的两个关键约束：
  1) 原页直接 get_pixmap 渲染（不经 show_pdf_page 归一化，避免丢失印章等透明组内容）；
  2) ImageBrush Viewbox 填位图自然尺寸（像素 × 96 / dpi，单位 1/96 英寸），尺寸适配在
     Viewport/Viewbox 层完成（contain-fit，等比居中不裁切）。
- 提交层：PowerShell 助手脚本 printshop/submit_xps.ps1（.NET 互操作选型见实施报告；
  pythonnet 在默认/core 运行时无法解析 System.Printing，仅硬编码 GAC 路径可用，故弃用），
  统一走 PrintQueue.CreateXpsDocumentWriter().Write（禁用 AddJob，柯美 PCL 队列挂起）。
"""
from __future__ import annotations

import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pymupdf as fitz

if TYPE_CHECKING:
    from printshop.print_engine import PrintOptions

A4_XPS_W = 793.7
A4_XPS_H = 1122.52  # A4 以 1/96 英寸计

_PS1_NAME = "submit_xps.ps1"

_CT = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="fdseq" ContentType="application/vnd.ms-package.xps-fixeddocumentsequence+xml"/>
<Default Extension="fdoc" ContentType="application/vnd.ms-package.xps-fixeddocument+xml"/>
<Default Extension="fpage" ContentType="application/vnd.ms-package.xps-fixedpage+xml"/>
<Default Extension="png" ContentType="image/png"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="utf-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="Rfdseq" Type="http://schemas.microsoft.com/xps/2005/06/fixedrepresentation" Target="/FixedDocumentSequence.fdseq"/>
</Relationships>"""

_FDSEQ = """<?xml version="1.0" encoding="utf-8"?>
<FixedDocumentSequence xmlns="http://schemas.microsoft.com/xps/2005/06">
<DocumentReference Source="/FixedDocument.fdoc"/>
</FixedDocumentSequence>"""

_FDOC_HEAD = """<?xml version="1.0" encoding="utf-8"?>
<FixedDocument xmlns="http://schemas.microsoft.com/xps/2005/06">
"""
_FDOC_TAIL = "</FixedDocument>"

_FPAGE_TMPL = """<?xml version="1.0" encoding="utf-8"?>
<FixedPage xmlns="http://schemas.microsoft.com/xps/2005/06" Width="{w}" Height="{h}" xml:lang="en-US">
<Path Data="M{vx},{vy} L{vx1},{vy} L{vx1},{vy1} L{vx},{vy1} Z">
<Path.Fill>
<ImageBrush ImageSource="/Images/{img}" ViewboxUnits="Absolute" ViewportUnits="Absolute" Viewbox="0,0,{vw},{vh}" Viewport="{vx},{vy},{vdw},{vdh}" TileMode="None"/>
</Path.Fill>
</Path>
</FixedPage>"""

_FPAGE_RELS_TMPL = """<?xml version="1.0" encoding="utf-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="Rimg" Type="http://schemas.microsoft.com/xps/2005/06/required-resource" Target="/Images/{img}"/>
</Relationships>"""

_DUPLEX_MAP = {
    "simplex": "OneSided",
    "duplexlong": "TwoSidedLongEdge",
    "duplexshort": "TwoSidedShortEdge",
}


def parse_pages(spec: str) -> list[int] | None:
    """把页码范围（如 "1-3,5"）解析为 1 起始页码列表；空/空白返回 None（=全部页）。

    支持逗号分隔的单个页码与 "a-b" 区间。结果保留书写顺序，未去重。
    """
    spec = spec.strip()
    if not spec:
        return None
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            pages.extend(range(int(a), int(b) + 1))
        else:
            pages.append(int(part))
    return pages


def _pixmap_natural_units(pix: fitz.Pixmap) -> tuple[float, float]:
    """位图自然尺寸（1/96 英寸）= 像素 × 96 / dpi。"""
    return pix.width * 96.0 / pix.xres, pix.height * 96.0 / pix.yres


def _fit_rect(nat_w: float, nat_h: float) -> tuple[float, float, float, float]:
    """contain：等比缩放完整放入 A4、居中、不裁切。返回 (x, y, w, h)。"""
    scale = min(A4_XPS_W / nat_w, A4_XPS_H / nat_h)
    w, h = nat_w * scale, nat_h * scale
    return (A4_XPS_W - w) / 2, (A4_XPS_H - h) / 2, w, h


def build_xps(pdf: Path, out: Path, dpi: int, pages: str = "") -> Path:
    """把 PDF 渲染打包为 XPS 文件。

    原页直接 get_pixmap(dpi=dpi) 渲染（含印章/注释，不经 show_pdf_page 归一化），
    尺寸适配在 XPS 层 Viewport/Viewbox 完成。pages 形如 "1-3,5"，空串=全部页。
    """
    pdf = Path(pdf)
    out = Path(out)
    selected = parse_pages(pages)
    with fitz.open(str(pdf)) as doc:
        if selected is None:
            indices = list(range(doc.page_count))
        else:
            indices = [i - 1 for i in selected if 1 <= i <= doc.page_count]
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            page_items: list[tuple[Path, float, float]] = []
            for i, idx in enumerate(indices, start=1):
                pix = doc[idx].get_pixmap(dpi=dpi)
                png = work / f"p{i}.png"
                pix.save(png)
                page_items.append((png, *_pixmap_natural_units(pix)))
            _write_xps_package(page_items, out)
    return out


def _write_xps_package(
    pages: list[tuple[Path, float, float]], xps_path: Path
) -> None:
    """把渲染位图写入合法 XPS（OPC）包。"""
    page_refs = "".join(
        f'<PageContent Source="/Pages/page{i}.fpage"/>\n'
        for i in range(1, len(pages) + 1)
    )
    with zipfile.ZipFile(xps_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("FixedDocumentSequence.fdseq", _FDSEQ)
        z.writestr("FixedDocument.fdoc", _FDOC_HEAD + page_refs + _FDOC_TAIL)
        for i, (png, nat_w, nat_h) in enumerate(pages, start=1):
            img = f"p{i}.png"
            vx, vy, vdw, vdh = _fit_rect(nat_w, nat_h)
            z.writestr(
                f"Pages/page{i}.fpage",
                _FPAGE_TMPL.format(
                    w=A4_XPS_W, h=A4_XPS_H, img=img,
                    vw=round(nat_w, 2), vh=round(nat_h, 2),
                    vx=round(vx, 2), vy=round(vy, 2),
                    vx1=round(vx + vdw, 2), vy1=round(vy + vdh, 2),
                    vdw=round(vdw, 2), vdh=round(vdh, 2),
                ),
            )
            z.writestr(f"Pages/_rels/page{i}.fpage.rels",
                       _FPAGE_RELS_TMPL.format(img=img))
            z.write(png, f"Images/{img}")


def print_ticket_xml(o: PrintOptions) -> str:
    """生成 PrintTicket XML（双面/彩色/份数/纸张，Print Schema 关键字）。"""
    duplex = _DUPLEX_MAP.get(o.duplex, "OneSided")
    color = "Color" if o.color else "Monochrome"
    paper = "ISOA4" if o.paper.upper() == "A4" else "ISOA4"
    copies = max(1, o.copies)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<psf:PrintTicket xmlns:psf="http://schemas.microsoft.com/windows/2003/08/printing/printschemaframework" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" version="1" xmlns:psk="http://schemas.microsoft.com/windows/2003/08/printing/printschemakeywords">
  <psf:ParameterInit name="psk:JobCopiesAllDocuments"><psf:Value xsi:type="xsd:integer">{copies}</psf:Value></psf:ParameterInit>
  <psf:Feature name="psk:PageOutputColor"><psf:Option name="psk:{color}" /></psf:Feature>
  <psf:Feature name="psk:JobDuplexAllDocumentsContiguously"><psf:Option name="psk:{duplex}" /></psf:Feature>
  <psf:Feature name="psk:PageMediaSize"><psf:Option name="psk:{paper}" /></psf:Feature>
</psf:PrintTicket>
"""


def submit_xps(xps: Path, o: PrintOptions) -> tuple[bool, str]:
    """调用 PowerShell 助手把 XPS 提交到打印机（XpsDocumentWriter.Write + PrintTicket）。

    PrintTicket 由 Python 侧生成（print_ticket_xml），写临时文件后交给 ps1 反序列化，
    保证双面/彩色/份数/纸张映射单点可测。退出码：0=成功；其余=失败（stderr 带详情）。
    """
    xps = Path(xps)
    ps1 = Path(__file__).resolve().parent / _PS1_NAME
    if not ps1.exists():
        return False, f"缺少提交助手脚本: {ps1}"
    with tempfile.NamedTemporaryFile(
        "w", suffix=".xml", encoding="utf-8", delete=False
    ) as tf:
        tf.write(print_ticket_xml(o))
        ticket_path = tf.name
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(ps1),
             "-XpsPath", str(xps), "-Printer", o.printer,
             "-TicketPath", ticket_path],
            capture_output=True, timeout=300,
        )
    except Exception as exc:
        return False, f"提交执行失败: {exc}"
    finally:
        Path(ticket_path).unlink(missing_ok=True)
    if r.returncode == 0:
        return True, ""
    if r.returncode == 4:
        return False, "打印机不存在"
    detail = (r.stderr or r.stdout or b"").decode("utf-8", "ignore").strip()
    return False, detail or f"打印失败（退出码 {r.returncode}）"
