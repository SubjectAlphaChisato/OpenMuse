"""Child-process PDF text extraction. Run as `python -m opa.artifacts.extract_worker MAX_CHARS`.

Reads PDF bytes on stdin, writes one JSON object on stdout. It has no database or network
access and is killed by the parent after the extraction timeout (ART-01).
"""
from __future__ import annotations

import io
import json
import sys

MIN_CHARS_PER_PAGE = 50  # below this on average the PDF is treated as scanned (needs OCR)


def extract(data: bytes, max_chars: int) -> dict:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            return {"status": "unsupported_format", "detail": "encrypted PDFs are not supported"}
        pages = len(reader.pages)
        if pages == 0:
            return {"status": "invalid_file", "detail": "the PDF has no pages"}
        parts: list[str] = []
        total = 0
        truncated = False
        for page in reader.pages:
            text = page.extract_text() or ""
            parts.append(text)
            total += len(text)
            if total >= max_chars:
                truncated = True
                break
    except (PdfReadError, ValueError, KeyError, TypeError, AttributeError) as e:
        return {"status": "invalid_file", "detail": f"the PDF could not be read ({type(e).__name__})"}
    text = "\n\n".join(parts)
    meaningful = sum(len(p.strip()) for p in parts)
    if not truncated and meaningful / pages < MIN_CHARS_PER_PAGE:
        return {"status": "unsupported_format", "pages": pages,
                "detail": "this PDF has little or no extractable text (it looks scanned); OCR is not supported in this version"}
    return {"status": "ok", "pages": pages, "text": text[:max_chars], "truncated": truncated or len(text) > max_chars}


def main() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):
        pass
    max_chars = int(sys.argv[1]) if len(sys.argv) > 1 else 2_000_000
    data = sys.stdin.buffer.read()
    sys.stdout.write(json.dumps(extract(data, max_chars)))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
