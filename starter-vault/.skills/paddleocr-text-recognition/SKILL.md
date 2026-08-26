---
name: paddleocr-text-recognition
description: Extract machine-readable text from images, screenshots, scans, or image-only PDF pages with PaddleOCR. Use when exact visible strings matter and native text extraction is absent or unreliable.
---

# PaddleOCR Text Recognition

1. Check whether the current Agent already has reliable visual or OCR capability. Use this Skill when deterministic text extraction adds value.
2. Prefer a local, no-upload path when available:

```bash
python3 .skills/paddleocr-text-recognition/scripts/local_ocr.py "<image>"
```

3. If PaddleOCR or its models are missing, report the dependency. Do not silently upload private images to a cloud API.
4. Preserve reading order where possible and flag uncertain characters, columns, rotated text, handwriting, and cropped regions.
5. Label output as `[OCR:PaddleOCR-local]`, `[OCR:PaddleOCR-api]`, or `[Unverified]`. OCR text does not prove full understanding of layout, icons, or interaction.
6. Verify consequential numbers, names, dates, amounts, IDs, and status labels against the source image.
