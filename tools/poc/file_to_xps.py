"""PoC: 通用 文件→XPS 打包（图片 contain-fit 不裁切；PDF 原页 300dpi 渲染）。

用法: python file_to_xps.py <输入文件> <输出.xps>
支持: .pdf / 图片（png/jpg 等 PyMuPDF 可读格式）
"""
import sys
import zipfile
from pathlib import Path

import pymupdf

A4_XPS_W, A4_XPS_H = 793.7, 1122.52        # A4 以 1/96 英寸计
DPI = 300

CT = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="fdseq" ContentType="application/vnd.ms-package.xps-fixeddocumentsequence+xml"/>
<Default Extension="fdoc" ContentType="application/vnd.ms-package.xps-fixeddocument+xml"/>
<Default Extension="fpage" ContentType="application/vnd.ms-package.xps-fixedpage+xml"/>
<Default Extension="png" ContentType="image/png"/>
</Types>"""

RELS = """<?xml version="1.0" encoding="utf-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="Rfdseq" Type="http://schemas.microsoft.com/xps/2005/06/fixedrepresentation" Target="/FixedDocumentSequence.fdseq"/>
</Relationships>"""

FDSEQ = """<?xml version="1.0" encoding="utf-8"?>
<FixedDocumentSequence xmlns="http://schemas.microsoft.com/xps/2005/06">
<DocumentReference Source="/FixedDocument.fdoc"/>
</FixedDocumentSequence>"""

FDOC_HEAD = """<?xml version="1.0" encoding="utf-8"?>
<FixedDocument xmlns="http://schemas.microsoft.com/xps/2005/06">
"""
FDOC_TAIL = "</FixedDocument>"

FPAGE_TMPL = """<?xml version="1.0" encoding="utf-8"?>
<FixedPage xmlns="http://schemas.microsoft.com/xps/2005/06" Width="{w}" Height="{h}" xml:lang="en-US">
<Path Data="M{vx},{vy} L{vx1},{vy} L{vx1},{vy1} L{vx},{vy1} Z">
<Path.Fill>
<ImageBrush ImageSource="/Images/{img}" ViewboxUnits="Absolute" ViewportUnits="Absolute" Viewbox="0,0,{vw},{vh}" Viewport="{vx},{vy},{vdw},{vdh}" TileMode="None"/>
</Path.Fill>
</Path>
</FixedPage>"""

FPAGE_RELS_TMPL = """<?xml version="1.0" encoding="utf-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="Rimg" Type="http://schemas.microsoft.com/xps/2005/06/required-resource" Target="/Images/{img}"/>
</Relationships>"""


def fit_rect(img_w: float, img_h: float) -> tuple[float, float, float, float]:
    """contain：等比缩放完整放入 A4、居中、不裁切。返回 (x, y, w, h)。"""
    scale = min(A4_XPS_W / img_w, A4_XPS_H / img_h)
    w, h = img_w * scale, img_h * scale
    return (A4_XPS_W - w) / 2, (A4_XPS_H - h) / 2, w, h


def pixmap_natural_units(pix: pymupdf.Pixmap) -> tuple[float, float]:
    """位图自然尺寸（1/96 英寸）= 像素 * 96 / dpi。"""
    return pix.width * 96.0 / pix.xres, pix.height * 96.0 / pix.yres


def pages_from_pdf(path: Path, work: Path) -> list[tuple[Path, float, float]]:
    doc = pymupdf.open(path)
    out = []
    for i, page in enumerate(doc, start=1):
        pix = page.get_pixmap(dpi=DPI)  # 原页直接渲染，含印章/注释
        png = work / f"pdf_p{i}.png"
        pix.save(png)
        out.append((png, *pixmap_natural_units(pix)))
    return out


def pages_from_image(path: Path, work: Path) -> list[tuple[Path, float, float]]:
    pix = pymupdf.Pixmap(str(path))
    if pix.alpha:  # 去透明通道，白底
        pix = pymupdf.Pixmap(pix, 0)
    png = work / "img_p1.png"
    pix.save(png)
    return [(png, *pixmap_natural_units(pix))]


def build_xps(pages: list[tuple[Path, float, float]], xps_path: Path) -> Path:
    page_refs = "".join(
        f'<PageContent Source="/Pages/page{i}.fpage"/>\n' for i in range(1, len(pages) + 1)
    )
    with zipfile.ZipFile(xps_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CT)
        z.writestr("_rels/.rels", RELS)
        z.writestr("FixedDocumentSequence.fdseq", FDSEQ)
        z.writestr("FixedDocument.fdoc", FDOC_HEAD + page_refs + FDOC_TAIL)
        for i, (png, nat_w, nat_h) in enumerate(pages, start=1):
            img = f"p{i}.png"
            vx, vy, vdw, vdh = fit_rect(nat_w, nat_h)
            z.writestr(
                f"Pages/page{i}.fpage",
                FPAGE_TMPL.format(
                    w=A4_XPS_W, h=A4_XPS_H, img=img,
                    vw=round(nat_w, 2), vh=round(nat_h, 2),
                    vx=round(vx, 2), vy=round(vy, 2),
                    vx1=round(vx + vdw, 2), vy1=round(vy + vdh, 2),
                    vdw=round(vdw, 2), vdh=round(vdh, 2),
                ),
            )
            z.writestr(f"Pages/_rels/page{i}.fpage.rels", FPAGE_RELS_TMPL.format(img=img))
            z.write(png, f"Images/{img}")
    return xps_path


def main() -> None:
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    work = dst.parent / (dst.stem + "_work")
    work.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() == ".pdf":
        pages = pages_from_pdf(src, work)
    else:
        pages = pages_from_image(src, work)
    build_xps(pages, dst)
    print(dst)


if __name__ == "__main__":
    main()
