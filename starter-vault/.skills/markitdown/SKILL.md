---
name: markitdown
description: Convert local or URL-based documents into LLM-friendly Markdown with Microsoft MarkItDown. Use for PDF, Word, PowerPoint, spreadsheet, HTML, CSV, JSON, XML, EPUB, image, audio, or archive extraction when layout-perfect rendering is not the primary requirement.
---

# MarkItDown

1. Confirm the input and intended output path. Preserve the original source.
2. Check `markitdown --version`; if unavailable, report the missing dependency instead of claiming conversion.
3. Convert to a temporary or explicitly authorized destination, for example:

```bash
markitdown "<input>" -o "<output.md>"
```

4. Inspect the result for missing tables, images, formulas, slide order, encoding, and truncated content. Mark limitations.
5. For scanned or layout-heavy documents, route to OCR or document parsing instead of treating an empty text result as success.
6. Place raw conversions under `20-资料/` or the chosen source area; summarize separately before publishing conclusions.

Do not overwrite the source, create uncontrolled sidecar directories, or extract secrets from protected files without explicit authority.
