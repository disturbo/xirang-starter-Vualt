#!/usr/bin/env python3
"""Run local PaddleOCR text recognition and print extracted text.

Execute with:
  uv tool run --from paddleocr python scripts/local_ocr.py /path/to/image.png
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
from typing import Any

from paddleocr import PaddleOCR


@contextlib.contextmanager
def suppress_native_output():
    with open(os.devnull, "w", encoding="utf-8") as devnull:
        old_stdout = os.dup(1)
        old_stderr = os.dup(2)
        try:
            os.dup2(devnull.fileno(), 1)
            os.dup2(devnull.fileno(), 2)
            yield
        finally:
            os.dup2(old_stdout, 1)
            os.dup2(old_stderr, 2)
            os.close(old_stdout)
            os.close(old_stderr)


def result_to_dict(result: Any) -> dict[str, Any]:
    data = getattr(result, "json", None)
    if callable(data):
        data = data()
    if isinstance(data, dict):
        return data
    if isinstance(result, dict):
        return result
    return {"raw": str(result)}


def extract_texts(data: dict[str, Any]) -> list[str]:
    payload = data.get("res", data)
    texts = payload.get("rec_texts", [])
    return [str(text) for text in texts]


def main() -> int:
    parser = argparse.ArgumentParser(description="Local PaddleOCR text extraction")
    parser.add_argument("input", help="Image or PDF path")
    parser.add_argument("--lang", default="ch", help="PaddleOCR language hint, default: ch")
    parser.add_argument("--det-model", default="PP-OCRv5_mobile_det")
    parser.add_argument("--rec-model", default=None)
    parser.add_argument("--server", action="store_true", help="Use server models instead of mobile models")
    parser.add_argument("--json", action="store_true", help="Print compact JSON instead of plain text")
    parser.add_argument("--full-json", action="store_true", help="Print full PaddleOCR result JSON")
    parser.add_argument("--output", help="Optional output file")
    parser.add_argument("--preprocess", action="store_true", help="Enable orientation/unwarping preprocessing")
    parser.add_argument("--verbose", action="store_true", help="Show PaddleOCR model loading logs")
    args = parser.parse_args()

    input_path = Path(args.input).expanduser()
    if not input_path.exists():
        raise SystemExit(f"Input file not found: {input_path}")

    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

    det_model = "PP-OCRv5_server_det" if args.server else args.det_model
    if args.rec_model:
        rec_model = args.rec_model
    elif args.server:
        rec_model = "PP-OCRv5_server_rec"
    elif args.lang.lower().startswith("en"):
        rec_model = "en_PP-OCRv5_mobile_rec"
    else:
        rec_model = "PP-OCRv5_mobile_rec"

    def run_ocr() -> list[dict[str, Any]]:
        ocr = PaddleOCR(
            text_detection_model_name=det_model,
            text_recognition_model_name=rec_model,
            use_doc_orientation_classify=args.preprocess,
            use_doc_unwarping=args.preprocess,
            use_textline_orientation=args.preprocess,
        )
        return [result_to_dict(page) for page in ocr.predict(str(input_path))]

    if args.verbose:
        pages = run_ocr()
    else:
        with suppress_native_output():
            pages = run_ocr()
    texts_by_page = [extract_texts(page) for page in pages]

    if args.full_json:
        rendered = json.dumps({"pages": pages}, ensure_ascii=False, indent=2)
    elif args.json:
        rendered = json.dumps({"pages": texts_by_page}, ensure_ascii=False, indent=2)
    else:
        chunks: list[str] = []
        for index, texts in enumerate(texts_by_page, start=1):
            if len(texts_by_page) > 1:
                chunks.append(f"--- page {index} ---")
            chunks.extend(texts)
        rendered = "\n".join(chunks)

    if args.output:
        Path(args.output).expanduser().write_text(rendered, encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
