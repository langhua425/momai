"""文本抽取 v4：pdf / office(含变体) / ofd / html / mht / rtf / odf / dbf / 纯文本 / 魔数嗅探。

返回 (text, kind, note)；kind 用于统计与 OCR 决策：
  text  纯文本    pdf  可抽文本的 PDF    pdf-scan  需要 OCR 的扫描件
  office  Office 文档    ofd  OFD 版式文件    unsupported 明确不支持    failed
"""
import email
import email.policy
import html as _html
import os
import re
import shutil
import struct
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET

TEXT_EXTS = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".log", ".ini", ".yml", ".yaml",
    ".sql", ".py", ".js", ".css", ".java", ".cs", ".sh", ".bat", ".ps1", ".toml", ".conf", ".cfg",
}
HTML_EXTS = {".html", ".htm", ".xhtml", ".shtml"}
MHTML_EXTS = {".mht", ".mhtml"}
RTF_EXTS = {".rtf"}
DBF_EXTS = {".dbf"}
PDF_EXTS = {".pdf"}
ODF_EXTS = {".odt", ".ods", ".odp", ".odg"}
OOXML_DISPATCH = {
    ".docx": "docx", ".docm": "docx", ".dotx": "docx", ".dot": "docx",
    ".xlsx": "xlsx", ".xlsm": "xlsx", ".xltx": "xlsx", ".xlt": "xlsx",
    ".pptx": "pptx", ".pptm": "pptx", ".potx": "pptx", ".pot": "pptx",
    ".ppsx": "pptx", ".pps": "pptx",
}
LEGACY_OFFICE = {".doc", ".xls", ".ppt", ".wps", ".et", ".dps", ".vsd"}
UNSUPPORTABLE = {
    ".caj": "知网 CAJ（需 CAJViewer，Windows）",
    ".ceb": "方正 Apabi（需 Apabi Reader，Windows）",
    ".kdh": "知网 KDH（需专有阅读器）",
    ".nh": "知网 NH（需专有阅读器）",
}


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
        txt = _clean("\n".join(parts))
        if txt:
            return txt, "office", ""
    except Exception:
        pass
    return _libreoffice_text(path) or ("", "failed", "docx 解析失败")


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
        txt = _clean("\n".join(parts))
        if txt:
            return txt, "office", ""
    except Exception:
        pass
    return _libreoffice_text(path) or ("", "failed", "xlsx 解析失败")


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
        txt = _clean("\n".join(parts))
        if txt:
            return txt, "office", ""
    except Exception:
        pass
    return _libreoffice_text(path) or ("", "failed", "pptx 解析失败")


SPREADSHEET_EXTS = {".xls", ".xlsx", ".xlsm", ".xlt", ".xltx", ".et", ".ods", ".csv"}


def _libreoffice_text(path: str, kind="office"):
    """libreoffice 无头转换；表格必须用 csv 过滤器（txt 过滤器不支持 Calc）。"""
    exe = shutil.which("libreoffice") or shutil.which("soffice")
    if not exe:
        return None
    ext = os.path.splitext(path)[1].lower()
    is_sheet = ext in SPREADSHEET_EXTS
    # 44=逗号 34=引号 76=UTF-8
    filters = (["csv:Text - txt - csv (StarCalc):44,34,76,1,,0,false,true,true",
                "csv"], "csv") if is_sheet else (["txt:Text (encoded):UTF8"], "txt")
    wanted, suffix = filters
    base = tempfile.mkdtemp()
    outdir = os.path.join(base, "out")
    homedir = os.path.join(base, "home")
    os.makedirs(outdir, exist_ok=True)
    os.makedirs(homedir, exist_ok=True)
    try:
        for f in wanted:
            shutil.rmtree(outdir, ignore_errors=True)
            os.makedirs(outdir, exist_ok=True)
            subprocess.run([exe, "--headless", "--norestore", "--convert-to", f,
                            "--outdir", outdir, path],
                           capture_output=True, timeout=600, env={**os.environ, "HOME": homedir})
            outs = [x for x in os.listdir(outdir) if x.lower().endswith("." + suffix)]
            if not outs:
                continue
            txt = _clean(read_plain(os.path.join(outdir, outs[0])))
            if txt:
                return txt, kind, f"由 libreoffice({suffix}) 转换"
    except Exception:
        pass
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return None


def _cjk_ratio(t: str) -> float:
    if not t:
        return 0.0
    sample = t[:4000]
    cjk = sum(1 for ch in sample if "\u4e00" <= ch <= "\u9fff")
    return cjk / max(1, len(sample))


def extract_legacy_office(path: str):
    """老二进制格式：libreoffice → antiword/catdoc → strings。"""
    r = _libreoffice_text(path)
    if r:
        return r
    ext = os.path.splitext(path)[1].lower()
    candidates = []
    if ext == ".doc":
        if shutil.which("antiword"):
            candidates.append(["antiword", path])
        if shutil.which("catdoc"):
            candidates.append(["catdoc", path])
    if shutil.which("catdoc"):
        candidates.append(["catdoc", "-d", "utf-8", path])
    for args in candidates:
        try:
            out = subprocess.run(args, capture_output=True, timeout=180)
            txt = _clean(out.stdout.decode("utf-8", errors="replace"))
            if len(txt) > 30:
                return txt, "office", f"由 {args[0]} 解析"
        except Exception:
            continue
    try:
        out = subprocess.run(["strings", "-n", "6", path], capture_output=True, timeout=120)
        txt = _clean(out.stdout.decode("utf-8", errors="replace"))
        if len(txt) > 50:
            # 判断是不是真文本：中文字符占比 + 可读字符占比
            latin_words = re.findall(r"[A-Za-z]{3,}", txt[:4000])
            if _cjk_ratio(txt) > 0.02 or len(latin_words) > 30:
                return txt, "office", "strings 兜底（可能有乱码）"
            return "", "failed", "无文本层（内容可能是嵌入图片，需 OCR）"
    except Exception:
        pass
    return "", "failed", "老格式无法解析"


def extract_odf(path: str):
    r = _libreoffice_text(path)
    if r:
        return r
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("content.xml")
        root = ET.fromstring(xml)
        parts = [el.text.strip() for el in root.iter() if el.text and el.text.strip()]
        txt = _clean("\n".join(parts))
        return (txt, "office", "ODF content.xml") if txt else ("", "failed", "ODF 无文本")
    except Exception as e:
        return "", "failed", f"ODF: {e}"


def extract_ofd(path: str):
    try:
        z = zipfile.ZipFile(path)
    except Exception as e:
        return "", "failed", f"ofd 打不开: {e}"
    parts = []
    with z:
        names = z.namelist()
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


def _strip_html(t: str) -> str:
    t = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", t)
    t = re.sub(r"(?is)<!--.*?-->", " ", t)
    t = re.sub(r"(?i)</(p|div|tr|h[1-6]|li)\s*>", "\n", t)
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return _clean(_html.unescape(t))


def extract_html(path: str):
    try:
        raw = open(path, "rb").read()
        enc = "utf-8"
        m = re.search(rb'charset=["\']?([\w-]+)', raw[:4096], re.I)
        if m:
            enc = m.group(1).decode("ascii", "ignore")
        try:
            t = raw.decode(enc, errors="replace")
        except Exception:
            t = raw.decode("utf-8", errors="replace")
        txt = _strip_html(t)
        return (txt, "text", "") if txt else ("", "failed", "HTML 无文本")
    except Exception as e:
        return "", "failed", f"html: {e}"


def extract_mhtml(path: str):
    try:
        msg = email.message_from_bytes(open(path, "rb").read(), policy=email.policy.default)
        plains, htmls = [], []
        for part in msg.walk():
            ct = (part.get_content_type() or "").lower()
            if part.get_content_maintype() == "multipart":
                continue
            # 用原始字节 + 多重编码回退，避免 get_content() 按错误 charset 解出乱码
            raw = part.get_payload(decode=True)
            if raw is None:
                continue
            body = None
            cs = (part.get_content_charset() or "").lower()
            for enc in ([cs] if cs else []) + ["utf-8", "gb18030", "utf-16"]:
                if not enc:
                    continue
                try:
                    body = raw.decode(enc)
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            if body is None:
                body = raw.decode("utf-8", errors="replace")
            # 明显乱码（大量替换符/私用区）→ 用 gb18030 再试
            bad = sum(1 for ch in body[:400] if ch == "\ufffd" or "\ue000" <= ch <= "\uf8ff")
            if bad > 20:
                try:
                    alt = raw.decode("gb18030")
                    if sum(1 for ch in alt[:400] if ch == "\ufffd") < 5:
                        body = alt
                except Exception:
                    pass
            if ct == "text/plain":
                plains.append(body)
            elif ct == "text/html":
                htmls.append(_strip_html(body))
        txt = _clean("\n".join(plains) if plains else "\n".join(htmls))
        return (txt, "text", "mht 解析") if txt else ("", "failed", "MHT 无文本")
    except Exception as e:
        return "", "failed", f"mht: {e}"


def extract_rtf(path: str):
    if shutil.which("unrtf"):
        try:
            out = subprocess.run(["unrtf", "--text", "--nopict", path],
                                 capture_output=True, timeout=180)
            txt = _clean(out.stdout.decode("utf-8", errors="replace"))
            if len(txt) > 20:
                return txt, "text", "由 unrtf 解析"
        except Exception:
            pass
    try:
        return _clean(re.sub(r"\\[a-z]+-?\d* ?", " ", read_plain(path))), "text", "rtf 粗解析"
    except Exception as e:
        return "", "failed", str(e)


def extract_dbf(path: str):
    """极简 dBase III 读取：字段描述 + 记录 → 制表符文本。"""
    try:
        raw = open(path, "rb").read()
        if len(raw) < 32:
            return "", "failed", "dbf 太短"
        nrec, hlen, rlen = struct.unpack("<I H H", raw[4:12])
        fields, pos = [], 32
        while pos + 32 <= hlen and raw[pos] != 0x0D:
            fd = raw[pos:pos + 32]
            name = fd[:11].split(b"\x00")[0].decode("gb18030", "replace").strip()
            fields.append((name, chr(fd[11]), fd[16]))
            pos += 32
        if not fields:
            return "", "failed", "dbf 无字段"
        out = ["\t".join(f[0] for f in fields)]
        for i in range(min(nrec, 200000)):
            start = hlen + i * rlen
            rec = raw[start:start + rlen]
            if len(rec) < rlen:
                break
            if rec[:1] == b"*":
                continue
            vals, off = [], 1
            for _, ftype, flen in fields:
                v = rec[off:off + flen]
                off += flen
                vals.append(v.decode("gb18030", "replace").strip())
            out.append("\t".join(vals))
        txt = _clean("\n".join(out))
        return txt, "text", f"dbf 属性表 {len(fields)} 字段 / {nrec} 条"
    except Exception as e:
        return "", "failed", f"dbf: {e}"


def _looks_text(raw: bytes) -> bool:
    if not raw:
        return False
    sample = raw[:4096]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        pass
    printable = sum(1 for b in sample if 9 <= b <= 13 or 32 <= b <= 126 or b >= 0x81)
    return printable / len(sample) > 0.90


def sniff(path: str):
    """按文件头判断真实类型。"""
    try:
        head = open(path, "rb").read(8)
    except Exception as e:
        return "", "failed", f"读头失败: {e}"
    if head[:5] == b"%PDF-":
        return extract_pdf(path)
    if head[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
            if "OFD.xml" in names:
                return extract_ofd(path)
            if "[Content_Types].xml" in names:
                if any(n.startswith("word/") for n in names):
                    return extract_docx(path)
                if any(n.startswith("xl/") for n in names):
                    return extract_xlsx(path)
                if any(n.startswith("ppt/") for n in names):
                    return extract_pptx(path)
            if "content.xml" in names and "mimetype" in names:
                return extract_odf(path)
            return "", "failed", "无名 ZIP 容器（无法判断内部格式）"
        except Exception as e:
            return "", "failed", f"zip 判断失败: {e}"
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return _libreoffice_text(path) or ("", "failed", "OLE 复合文档（libreoffice 未识别）")
    if head[:5] == b"{\\rtf":
        return extract_rtf(path)
    if head[:2] == b"\x1f\x8b":
        return "", "failed", "gzip 压缩体（应由解包器处理）"
    raw = open(path, "rb").read(8192)
    if _looks_text(raw):
        return _clean(read_plain(path)), "text", "无扩展名，按文本读取"
    return "", "failed", f"无法识别（文件头 {head.hex()}）"


def extract(path: str):
    """统一入口 → (text, kind, note)"""
    ext = os.path.splitext(path)[1].lower()

    if ext in TEXT_EXTS:
        return _clean(read_plain(path)), "text", ""
    if ext in HTML_EXTS:
        return extract_html(path)
    if ext in MHTML_EXTS:
        return extract_mhtml(path)
    if ext in PDF_EXTS:
        return extract_pdf(path)
    if ext in OOXML_DISPATCH:
        return {"docx": extract_docx, "xlsx": extract_xlsx, "pptx": extract_pptx}[
            OOXML_DISPATCH[ext]](path)
    if ext == ".ofd":
        return extract_ofd(path)
    if ext in ODF_EXTS:
        return extract_odf(path)
    if ext in LEGACY_OFFICE:
        return extract_legacy_office(path)
    if ext in RTF_EXTS:
        return extract_rtf(path)
    if ext in DBF_EXTS:
        return extract_dbf(path)
    if ext in UNSUPPORTABLE:
        return "", "unsupported", UNSUPPORTABLE[ext]
    return sniff(path)
