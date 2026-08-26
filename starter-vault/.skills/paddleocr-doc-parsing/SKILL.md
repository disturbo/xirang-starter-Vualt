---
name: paddleocr-doc-parsing
description: Parse scanned PDFs and complex document images into structured Markdown or JSON, including tables, formulas, reading order, figures, and seals. Use when plain OCR cannot preserve document structure.
---

# PaddleOCR Document Parsing

1. Inspect whether the file already has a usable text layer. Use structural parsing only when layout or document objects matter.
2. Check the available PaddleOCR document-parsing command or API without exposing credentials.
3. Prefer local processing for sensitive material. Cloud parsing requires explicit authority to upload and a currently configured credential.
4. Preserve page boundaries, headings, table cells, formulas, captions, reading order, and references to figures.
5. Mark uncertain cells and visually inferred relationships. Validate critical fields against rendered pages.
6. Save raw parser output separately from the edited summary, with the original file and page pointers retained.

If only text OCR is available, route to `paddleocr-text-recognition` and describe the structural loss honestly.
