"""PoC v2: PDF 原页直接渲染 → XPS 包（系统打印服务 PrintTicket 路径验证双面+彩色+印章）。

要点：
- 不经 show_pdf_page 归一化（会丢印章等内容流对象），原始页面直接渲染位图；
- 页面尺寸适配在 XPS 层做（Viewport=适配矩形，Viewbox=位图自然尺寸），
  自然尺寸 = 像素 * 96 / dpi（PyMuPDF 会写 pHYs，XPS 按它换算自然尺寸）。
"""
import zipfile
from pathlib import Path

import pymupdf

SRC = Path(r"E:\kaifa\自助打印\调查令-30(1).pdf")
OUT = Path(r"E:\kaifa\自助打印\tools\poc")
OUT.mkdir(parents=True, exist_ok=True)

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
    """把图像自然尺寸（1/96 英寸）等比缩放进 A4 并居中，返回 (x, y, w, h)。"""
    scale = min(A4_XPS_W / img_w, A4_XPS_H / img_h)
    w, h = img_w * scale, img_h * scale
    return (A4_XPS_W - w) / 2, (A4_XPS_H - h) / 2, w, h


def main() -> None:
    src = pymupdf.open(SRC)

    # 原始页面直接渲染（含印章等内容流与注释），不经 show_pdf_page
    pages = []
    for i, p in enumerate(src, start=1):
        pix = p.get_pixmap(dpi=DPI)  # annots=True 默认开启
        png = OUT / f"q{i}.png"
        pix.save(png)
        nat_w = pix.width * 96.0 / pix.xres
        nat_h = pix.height * 96.0 / pix.yres
        pages.append((png, nat_w, nat_h))

    xps_path = OUT / "poc-duplex-v2.xps"
    page_refs = "".join(
        f'<PageContent Source="/Pages/page{i}.fpage"/>\n' for i in range(1, len(pages) + 1)
    )
    with zipfile.ZipFile(xps_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CT)
        z.writestr("_rels/.rels", RELS)
        z.writestr("FixedDocumentSequence.fdseq", FDSEQ)
        z.writestr("FixedDocument.fdoc", FDOC_HEAD + page_refs + FDOC_TAIL)
        for i, (png, nat_w, nat_h) in enumerate(pages, start=1):
            img = f"q{i}.png"
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
    print(xps_path)


if __name__ == "__main__":
    main()
