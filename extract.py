"""文本抽取：pdf / docx / xlsx / pptx / ofd / 纯文本 / rtf 等。

返回 (text, kind, note)；kind 用于统计与 OCR 决策：
  text  纯文本    pdf  可抽文本的 PDF    pdf-scan  需要 OCR 的扫描件
  office  Office 文档    ofd  OFD 版式文件    failed
"""
import os
import re
import zipfile
import subprocess
import shutil
import tempfile
import xml.etree.ElementTree as ET

TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm", ".log", ".ini", ".yml", ".yaml", ".sql", ".py", ".js", ".css", ".java", ".cs", ".sh"}
OFFICE_EXTS = {".docx", ".xlsx", ".pptx"}
LEGACY_OFFICE = {".doc", ".xls", ".ppt"}
PDF_EXTS = {".pdf"}


def _clean(t: str) -> str:
    t = t.replace("\x00", " ")
    t = re.sub(r"[ \t\u3000]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def read_plain(path: str) -> str:
    raw = open(path, "rb").read()
    for enc in ("utf-8", "utf-8-sig", "gb18030", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_pdf(path: str, min_chars: int = 80):
    if not shutil.which("pdftotext"):
        return "", "pdf", "缺少 pdftotext"
    try:
        out = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", path, "-"],
                             capture_output=True, timeout=180)
        txt = _clean(out.stdout.decode("utf-8", errors="replace"))
    except Exception as e:
        return "", "pdf", f"pdftotext 失败: {e}"
    if len(txt) < min_chars:
        return txt, "pdf-scan", "文本过少，判定为扫描件"
    return txt, "pdf", ""


def extract_docx(path: str):
    try:
        import docx
        d = docx.Document(path)
        parts = [p.text for p in d.paragraphs if p.text.strip()]
        for t in d.tables:
            for row in t.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        return _clean("\n".join(parts)), "office", ""
    except Exception as e:
        return "", "failed", f"docx: {e}"


def extract_xlsx(path: str):
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets:
            parts.append(f"## {ws.title}")
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None and str(c).strip()]
                if cells:
                    parts.append(" | ".join(cells))
        wb.close()
        return _clean("\n".join(parts)), "office", ""
    except Exception as e:
        return "", "failed", f"xlsx: {e}"


def extract_pptx(path: str):
    try:
        from pptx import Presentation
        prs = Presentation(path)
        parts = []
        for i, slide in enumerate(prs.slides, 1):
            parts.append(f"## 第 {i} 页")
            for shape in slide.shapes:
                if shape.has_text_frame and shape.text_frame.text.strip():
                    parts.append(shape.text_frame.text.strip())
        return _clean("\n".join(parts)), "office", ""
    except Exception as e:
        return "", "failed", f"pptx: {e}"


def extract_ofd(path: str):
    """OFD 是 zip 容器：解析 Content.xml / Document.xml / Pages/*/Content.xml 里的文本。"""
    try:
        z = zipfile.ZipFile(path)
    except Exception as e:
        return "", "failed", f"ofd 打不开: {e}"
    parts = []
    with z:
        names = z.namelist()
        # 先按页序找 Content.xml
        page_files = sorted([n for n in names if re.search(r"(Pages?|Doc_\d+).*Content\.xml$", n, re.I)])
        targets = page_files or [n for n in names if n.lower().endswith(".xml")]
        for n in targets[:2000]:
            try:
                data = z.read(n)
            except Exception:
                continue
            try:
                root = ET.fromstring(data)
            except ET.ParseError:
                continue
            for el in root.iter():
                tag = el.tag.split("}")[-1]
                if tag in ("TextCode", "Text", "Content"):
                    if el.text and el.text.strip():
                        parts.append(el.text.strip())
    txt = _clean("\n".join(parts))
    return txt, "ofd", ("" if txt else "OFD 内未找到文本层")


def extract_legacy_office(path: str):
    """老格式 .doc/.xls/.ppt：先试 catdoc/libreoffice，否则退回 strings。"""
    for tool, args in (("libreoffice", ["--headless", "--convert-to", "txt", "--outdir", "{d}", path]),
                       ("catdoc", [path])):
        exe = shutil.which(tool)
        if not exe:
            continue
        try:
            if tool == "libreoffice":
                d = tempfile.mkdtemp()
                subprocess.run([exe] + [a.replace("{d}", d) for a in args],
                               capture_output=True, timeout=180)
                for fn in os.listdir(d):
                    if fn.endswith(".txt"):
                        return _clean(read_plain(os.path.join(d, fn))), "office", f"由 {tool} 转换"
            else:
                out = subprocess.run([exe] + args, capture_output=True, timeout=120)
                return _clean(out.stdout.decode("utf-8", errors="replace")), "office", f"由 {tool} 解析"
        except Exception:
            continue
    try:
        out = subprocess.run(["strings", "-n", "6", path], capture_output=True, timeout=60)
        txt = _clean(out.stdout.decode("utf-8", errors="replace"))
        if len(txt) > 50:
            return txt, "office", "strings 兜底（可能有乱码）"
    except Exception:
        pass
    return "", "failed", "老格式无法解析"


def extract(path: str):
    """统一入口 → (text, kind, note)"""
    ext = os.path.splitext(path)[1].lower()
    if ext in TEXT_EXTS:
        return _clean(read_plain(path)), "text", ""
    if ext == ".pdf":
        return extract_pdf(path)
    if ext == ".docx":
        return extract_docx(path)
    if ext == ".xlsx":
        return extract_xlsx(path)
    if ext == ".pptx":
        return extract_pptx(path)
    if ext == ".ofd":
        return extract_ofd(path)
    if ext in LEGACY_OFFICE:
        return extract_legacy_office(path)
    if ext in (".rtf",):
        try:
            return _clean(re.sub(r"\\[a-z]+\d*\s?", " ", read_plain(path))), "text", "rtf 粗解析"
        except Exception as e:
            return "", "failed", str(e)
    return "", "failed", f"不支持的扩展名 {ext}"
