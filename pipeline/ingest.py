"""输入归一化：把 PDF / 图片（截图）/ 纯文本统一转成 text 字符串。

入口 `ingest(path)`，按扩展名分派：
- .pdf  → 先用 pdfplumber 取文字层；仅当文字层为空/近空时，才逐页渲染成图片走 OCR。
- 图片  → 直接走 rapidocr-onnxruntime OCR（纯 CPU 中文）。
- 文本  → 按文本读取（依次尝试 utf-8 / utf-8-sig / gb18030），并拒绝二进制伪装。

注意：
- rapidocr 首次运行会联网下载中文 OCR 模型（缓存在本地）。
- 扫描件 PDF 的渲染依赖 pdfplumber 的 pypdfium2 后端。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff", ".tif"}
TEXT_EXTS = {".txt", ".md", ".text"}

# 文字层判定阈值：非空白字符数达到该值才视为「有文字层」，否则走 OCR。
MIN_TEXT_CHARS = 10

_ocr_engine = None


def ingest(path: str | os.PathLike) -> str:
    """把任意输入文件转成纯文本。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {path}")

    ext = p.suffix.lower()
    if ext == ".pdf":
        text = _from_pdf(p)
    elif ext in IMAGE_EXTS:
        text = _ocr(str(p))
    elif ext in TEXT_EXTS:
        text = _from_text(p)
    else:
        raise ValueError(f"暂不支持的输入类型: {ext or '(无后缀)'}，支持 PDF / 图片 / txt")

    if not text or not text.strip():
        raise ValueError(f"未能从该输入中识别出任何文字: {path}")
    return text


def _from_text(path: Path) -> str:
    raw = path.read_bytes()
    if b"\x00" in raw[:4096]:
        raise ValueError(f"疑似二进制/非文本文件（如需支持 Word 请另开切片）: {path}")
    for enc in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc).strip()
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("utf-8", errors="replace").strip()


def _from_pdf(path: Path) -> str:
    try:
        import pdfplumber
        from pdfplumber.utils.exceptions import PdfminerException
    except ImportError as e:
        raise RuntimeError(
            "解析 PDF 需要 pdfplumber（含渲染后端 pypdfium2），请先 pip install pdfplumber pypdfium2"
        ) from e

    try:
        with pdfplumber.open(str(path)) as pdf:
            text = _pdf_text_layer(pdf)
            if _has_text_layer(text):
                return text
            return _pdf_ocr(pdf)
    except PdfminerException as e:
        raise ValueError(f"PDF 无法解析，文件可能损坏或加密: {path}") from e


def _pdf_text_layer(pdf) -> str:
    parts = [page.extract_text() or "" for page in pdf.pages]
    return "\n".join(parts)


def _pdf_ocr(pdf) -> str:
    """扫描件：逐页渲染成图片后 OCR。"""
    parts = []
    for page in pdf.pages:
        pil_img = page.to_image(resolution=300).original
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp = f.name
        try:
            pil_img.save(tmp)
            parts.append(_ocr(tmp))
        finally:
            os.unlink(tmp)
    return "\n".join(p for p in parts if p)


def _has_text_layer(text: str) -> bool:
    meaningful = "".join(ch for ch in text if not ch.isspace())
    return len(meaningful) >= MIN_TEXT_CHARS


def _ocr(image_path: str) -> str:
    engine = _get_ocr_engine()
    result, _ = engine(str(image_path))
    if not result:
        return ""
    lines = []
    for item in result:
        # rapidocr 返回 [box, text, score]
        text = item[1]
        if text and str(text).strip():
            lines.append(str(text).strip())
    return "\n".join(lines)


def _get_ocr_engine():
    global _ocr_engine
    if _ocr_engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as e:
            raise RuntimeError(
                "OCR 需要 rapidocr-onnxruntime，请先 pip install rapidocr-onnxruntime"
                "（首次运行会联网下载中文 OCR 模型）"
            ) from e
        _ocr_engine = RapidOCR()
    return _ocr_engine


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("用法: python -m pipeline.ingest <文件路径>")
        sys.exit(2)
    print(ingest(sys.argv[1]))
