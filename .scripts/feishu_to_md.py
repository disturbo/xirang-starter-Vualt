#!/usr/bin/env python3
"""
飞书文档 → Markdown 转换器
通过飞书 Open API 拉取 docx 文档的所有 blocks，转为 Markdown 格式。
支持自动下载图片到本地。
"""

import json, sys, os, re, urllib.request, urllib.parse, time

# Credentials are intentionally not shipped with the starter vault.
# Configure them locally with environment variables:
#   FEISHU_APP_ID / FEISHU_APP_SECRET
# or per tenant domain:
#   FEISHU_<TENANT>_APP_ID / FEISHU_<TENANT>_APP_SECRET

# ── helpers ──────────────────────────────────────────────

def api(method, path, body=None, token=None):
    url = f"https://open.feishu.cn/open-apis{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())

def get_token(app_id, app_secret):
    """Get tenant access token."""
    r = api("POST", "/auth/v3/tenant_access_token/internal",
            {"app_id": app_id, "app_secret": app_secret})
    return r["tenant_access_token"]

def detect_tenant(url):
    """Extract tenant domain from Feishu URL."""
    m = re.search(r'https?://([a-z0-9]+)\.feishu\.cn', url)
    if m:
        return m.group(1)
    return None

def env_key_for_tenant(domain):
    return re.sub(r"[^A-Z0-9]", "_", domain.upper())

def resolve_credentials(domain=None, cli_app_id=None, cli_app_secret=None):
    """Resolve Feishu app credentials without embedding tenant data in the repo."""
    if cli_app_id or cli_app_secret:
        if not (cli_app_id and cli_app_secret):
            raise ValueError("--app-id and --app-secret must be provided together")
        return cli_app_id, cli_app_secret, "command line"

    if domain:
        key = env_key_for_tenant(domain)
        tenant_app_id = os.getenv(f"FEISHU_{key}_APP_ID")
        tenant_app_secret = os.getenv(f"FEISHU_{key}_APP_SECRET")
        if tenant_app_id and tenant_app_secret:
            return tenant_app_id, tenant_app_secret, f"FEISHU_{key}_*"

    app_id = os.getenv("FEISHU_APP_ID")
    app_secret = os.getenv("FEISHU_APP_SECRET")
    if app_id and app_secret:
        return app_id, app_secret, "FEISHU_APP_ID/FEISHU_APP_SECRET"

    hint = [
        "Missing Feishu app credentials.",
        "Set FEISHU_APP_ID and FEISHU_APP_SECRET, or pass --app-id and --app-secret.",
    ]
    if domain:
        key = env_key_for_tenant(domain)
        hint.append(f"For this tenant, you can also set FEISHU_{key}_APP_ID and FEISHU_{key}_APP_SECRET.")
    raise ValueError(" ".join(hint))

def get_wiki_node(token, wiki_token):
    r = api("GET", f"/wiki/v2/spaces/get_node?token={wiki_token}", token=token)
    return r["data"]["node"]

def get_all_blocks(token, doc_id):
    blocks = []
    page_token = ""
    while True:
        path = f"/docx/v1/documents/{doc_id}/blocks?page_size=500"
        if page_token:
            path += f"&page_token={page_token}"
        r = api("GET", path, token=token)
        items = r.get("data", {}).get("items", [])
        blocks.extend(items)
        if not r.get("data", {}).get("has_more"):
            break
        page_token = r["data"]["page_token"]
        print(f"  ... fetched {len(blocks)} blocks", file=sys.stderr)
    return blocks

# ── image download ──────────────────────────────────────

def download_image(token, file_token, save_dir, index):
    """Download a single image from Feishu and return local filename."""
    url = f"https://open.feishu.cn/open-apis/drive/v1/medias/{file_token}/download"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
            ct = resp.headers.get("Content-Type", "image/png")
            # Determine extension
            ext = "png"
            if "jpeg" in ct or "jpg" in ct:
                ext = "jpg"
            elif "gif" in ct:
                ext = "gif"
            elif "webp" in ct:
                ext = "webp"
            elif "svg" in ct:
                ext = "svg"
            filename = f"img_{index:03d}.{ext}"
            filepath = os.path.join(save_dir, filename)
            with open(filepath, "wb") as f:
                f.write(data)
            return filename
    except Exception as e:
        print(f"  ⚠ Failed to download {file_token}: {e}", file=sys.stderr)
        return None

def download_all_images(token, blocks, save_dir):
    """Download all images from blocks, return token→filename mapping."""
    os.makedirs(save_dir, exist_ok=True)
    token_map = {}
    image_tokens = []

    # Collect all image tokens
    for b in blocks:
        if b.get("block_type") == 27:
            img = b.get("image", {})
            t = img.get("token", "")
            if t and t not in token_map:
                image_tokens.append(t)
                token_map[t] = None  # placeholder

    total = len(image_tokens)
    print(f"  Found {total} unique images to download", file=sys.stderr)

    for i, ft in enumerate(image_tokens):
        filename = download_image(token, ft, save_dir, i + 1)
        token_map[ft] = filename
        if (i + 1) % 20 == 0 or (i + 1) == total:
            print(f"  ... downloaded {i+1}/{total}", file=sys.stderr)
        # Small delay to avoid rate limiting
        if (i + 1) % 50 == 0:
            time.sleep(1)

    success = sum(1 for v in token_map.values() if v)
    print(f"  ✓ Downloaded {success}/{total} images", file=sys.stderr)
    return token_map

# ── block → markdown ─────────────────────────────────────

def text_elements_to_md(elements):
    """Convert a list of text elements to markdown string."""
    if not elements:
        return ""
    parts = []
    for el in elements:
        if "text_run" in el:
            tr = el["text_run"]
            content = tr.get("content", "")
            style = tr.get("text_element_style", {})
            # Apply inline styles
            if style.get("bold"):
                content = f"**{content}**"
            if style.get("italic"):
                content = f"*{content}*"
            if style.get("strikethrough"):
                content = f"~~{content}~~"
            if style.get("inline_code"):
                content = f"`{content}`"
            if style.get("link"):
                url = style["link"].get("url", "")
                # Feishu URLs are sometimes percent-encoded
                try:
                    url = urllib.parse.unquote(url)
                except:
                    pass
                content = f"[{content}]({url})"
            parts.append(content)
        elif "mention_doc" in el:
            md = el["mention_doc"]
            title = md.get("title", "文档")
            url = md.get("url", "")
            parts.append(f"[{title}]({url})")
        elif "equation" in el:
            parts.append(f"${el['equation'].get('content', '')}$")
    return "".join(parts)

def block_text(block):
    """Extract text content from a block's primary text field."""
    for key in ["text", "heading1", "heading2", "heading3", "heading4",
                "heading5", "heading6", "heading7", "heading8", "heading9",
                "code", "quote", "todo", "callout",
                "bullet", "ordered", "task"]:
        obj = block.get(key)
        if obj and "elements" in obj:
            return text_elements_to_md(obj["elements"])
    return ""

def ensure_blank_line(lines):
    """Ensure the last line in list is blank (for block separation)."""
    if lines and lines[-1].strip() != "":
        lines.append("")

def blocks_to_markdown(blocks, image_map=None, image_rel_dir=""):
    """Convert all blocks to markdown lines."""
    block_map = {b["block_id"]: b for b in blocks}
    lines = []

    # Track table state (type 31 = table in Feishu API)
    table_info = {}
    for b in blocks:
        bt = b.get("block_type")
        if bt == 31:  # table
            tbl = b.get("table", {})
            table_info[b["block_id"]] = {
                "rows": tbl.get("property", {}).get("row_size", 0),
                "cols": tbl.get("property", {}).get("column_size", 0),
                "cells": tbl.get("cells", []),
            }

    # Build parent map & identify blocks inside tables/grids
    parent_map = {}
    for b in blocks:
        for child_id in b.get("children", []):
            parent_map[child_id] = b["block_id"]

    # Collect all block IDs that are inside table cells (type 32)
    skip_ids = set()
    for tid, tinfo in table_info.items():
        for cell_id in tinfo["cells"]:
            skip_ids.add(cell_id)
            cell_block = block_map.get(cell_id, {})
            for child_id in cell_block.get("children", []):
                skip_ids.add(child_id)
                # Also handle nested children (2 levels deep)
                child_block = block_map.get(child_id, {})
                for gc_id in child_block.get("children", []):
                    skip_ids.add(gc_id)

    # Also skip grid_column children (type 25) — render inline
    for b in blocks:
        if b.get("block_type") == 25:  # grid_column
            skip_ids.add(b["block_id"])

    def render_cell(cell_id):
        """Render a table cell's content as inline text."""
        cell = block_map.get(cell_id, {})
        parts = []
        for child_id in cell.get("children", []):
            child = block_map.get(child_id, {})
            t = block_text(child)
            if t:
                parts.append(t.replace("\n", " ").replace("|", "\\|"))
        return " ".join(parts) if parts else " "

    for b in blocks:
        bid = b["block_id"]
        bt = b.get("block_type")

        if bid in skip_ids:
            continue

        # 1: Page (root) — skip
        if bt == 1:
            continue

        # 2: Text / paragraph
        elif bt == 2:
            t = block_text(b)
            lines.append(t)
            lines.append("")

        # 3-11: Headings (heading1 through heading9)
        elif 3 <= bt <= 11:
            level = bt - 2  # 3→h1, 4→h2, 5→h3, ...
            level = min(level, 6)
            t = block_text(b)
            if t.strip():
                ensure_blank_line(lines)
                lines.append(f"{'#' * level} {t.strip()}")
                lines.append("")

        # 12: Bullet list
        elif bt == 12:
            t = block_text(b)
            lines.append(f"- {t}")

        # 13: Ordered list
        elif bt == 13:
            t = block_text(b)
            lines.append(f"1. {t}")

        # 14: Code block
        elif bt == 14:
            code = b.get("code", {})
            lang = code.get("style", {}).get("language", "")
            lang_map = {1: "plaintext", 7: "bash", 8: "csharp", 9: "cpp",
                       10: "c", 12: "css", 22: "go", 25: "html", 28: "java",
                       29: "javascript", 30: "json", 36: "markdown", 40: "php",
                       42: "python", 46: "rust", 48: "shell", 50: "sql",
                       53: "swift", 56: "typescript", 60: "xml", 62: "yaml"}
            lang_str = lang_map.get(lang, "") if isinstance(lang, int) else str(lang)
            t = block_text(b)
            ensure_blank_line(lines)
            lines.append(f"```{lang_str}")
            lines.append(t)
            lines.append("```")
            lines.append("")

        # 15: Quote
        elif bt == 15:
            t = block_text(b)
            if t:
                ensure_blank_line(lines)
                for line in t.split("\n"):
                    lines.append(f"> {line}")
                lines.append("")

        # 16: Equation
        elif bt == 16:
            t = block_text(b)
            ensure_blank_line(lines)
            lines.append(f"$$\n{t}\n$$")
            lines.append("")

        # 17: Todo
        elif bt == 17:
            todo = b.get("todo", {})
            checked = todo.get("style", {}).get("done", False)
            t = block_text(b)
            mark = "x" if checked else " "
            lines.append(f"- [{mark}] {t}")

        # 18: Divider
        elif bt == 18:
            ensure_blank_line(lines)
            lines.append("---")
            lines.append("")

        # 19: Callout (NOT image!)
        elif bt == 19:
            t = block_text(b)
            if t:
                ensure_blank_line(lines)
                lines.append(f"> [!note]")
                for line in t.split("\n"):
                    lines.append(f"> {line}")
                lines.append("")

        # 24: Grid — skip container, children rendered separately
        elif bt == 24:
            continue

        # 25: Grid column — skip container
        elif bt == 25:
            continue

        # 27: Image
        elif bt == 27:
            img = b.get("image", {})
            token_val = img.get("token", "")
            ensure_blank_line(lines)
            if image_map and token_val in image_map and image_map[token_val]:
                local_file = image_map[token_val]
                if image_rel_dir:
                    lines.append(f"![[{image_rel_dir}/{local_file}]]")
                else:
                    lines.append(f"![[{local_file}]]")
            else:
                lines.append(f"![image]({token_val})")
            lines.append("")

        # 31: Table
        elif bt == 31:
            tinfo = table_info.get(bid, {})
            rows = tinfo.get("rows", 0)
            cols = tinfo.get("cols", 0)
            cells = tinfo.get("cells", [])

            if rows > 0 and cols > 0:
                # Always ensure blank line before table
                ensure_blank_line(lines)
                for r in range(rows):
                    row_cells = []
                    for c in range(cols):
                        idx = r * cols + c
                        cell_id = cells[idx] if idx < len(cells) else ""
                        row_cells.append(render_cell(cell_id))
                    lines.append("| " + " | ".join(row_cells) + " |")
                    if r == 0:
                        lines.append("|" + "|".join(["---"] * cols) + "|")
                lines.append("")

        # 32: Table cell — skip (handled by table)
        elif bt == 32:
            continue

        # 43: Board/diagram — placeholder
        elif bt == 43:
            lines.append("*(飞书画板/白板，无法转为文本)*")
            lines.append("")

        # Unknown — extract text if any
        else:
            t = block_text(b)
            if t:
                lines.append(t)
                lines.append("")

    return "\n".join(lines)

# ── main ─────────────────────────────────────────────────

def get_doc_meta(token, doc_id):
    """Get document metadata (title etc.) for a direct docx document."""
    r = api("GET", f"/docx/v1/documents/{doc_id}", token=token)
    doc = r.get("data", {}).get("document", {})
    return doc.get("title", "未命名文档")

def main():
    if len(sys.argv) < 2:
        print("Usage: feishu_to_md.py <feishu_url_or_token> [output.md] [--no-images] [--app-id ID] [--app-secret SECRET]", file=sys.stderr)
        print("  Supports /wiki/TOKEN and /docx/TOKEN URLs", file=sys.stderr)
        print("  Credentials: set FEISHU_APP_ID/FEISHU_APP_SECRET or pass --app-id/--app-secret", file=sys.stderr)
        sys.exit(1)

    url_or_token = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else None
    skip_images = "--no-images" in sys.argv

    # Parse --app-id and --app-secret
    cli_app_id = None
    cli_app_secret = None
    for i, arg in enumerate(sys.argv):
        if arg == "--app-id" and i + 1 < len(sys.argv):
            cli_app_id = sys.argv[i + 1]
        if arg == "--app-secret" and i + 1 < len(sys.argv):
            cli_app_secret = sys.argv[i + 1]

    # Detect URL type: /wiki/ or /docx/
    is_wiki = "/wiki/" in url_or_token
    is_docx = "/docx/" in url_or_token
    doc_token = url_or_token.split("/")[-1].split("?")[0]

    # Detect tenant from URL domain, then resolve credentials from local env/CLI.
    tenant_domain = detect_tenant(url_or_token)
    try:
        app_id, app_secret, credential_source = resolve_credentials(
            tenant_domain, cli_app_id, cli_app_secret
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    print(f"[1/5] Getting access token...", file=sys.stderr)
    if tenant_domain:
        print(f"  Tenant: {tenant_domain}.feishu.cn", file=sys.stderr)
    print(f"  Credential source: {credential_source}", file=sys.stderr)
    token = get_token(app_id, app_secret)

    if is_wiki or (not is_docx):
        # Wiki node → resolve to doc_id
        print(f"[2/5] Getting wiki node info for {doc_token}...", file=sys.stderr)
        node = get_wiki_node(token, doc_token)
        doc_id = node["obj_token"]
        title = node["title"]
        obj_type = node["obj_type"]
        source_url = url_or_token if url_or_token.startswith("http") else f"https://feishu.cn/wiki/{doc_token}"
        print(f"  Title: {title}", file=sys.stderr)
        print(f"  Type: {obj_type}, doc_id: {doc_id}", file=sys.stderr)
        if obj_type != "docx":
            print(f"  WARNING: obj_type is '{obj_type}', not 'docx'. May not work.", file=sys.stderr)
    else:
        # Direct docx → token IS the doc_id
        doc_id = doc_token
        print(f"[2/5] Getting docx metadata for {doc_id}...", file=sys.stderr)
        title = get_doc_meta(token, doc_id)
        source_url = url_or_token if url_or_token.startswith("http") else f"https://feishu.cn/docx/{doc_token}"
        print(f"  Title: {title}", file=sys.stderr)

    print(f"[3/5] Fetching all blocks...", file=sys.stderr)
    blocks = get_all_blocks(token, doc_id)
    print(f"  Total blocks: {len(blocks)}", file=sys.stderr)

    # Image download
    image_map = {}
    image_rel_dir = ""
    if not skip_images:
        print(f"[4/5] Downloading images...", file=sys.stderr)
        if output_file:
            out_dir = os.path.dirname(output_file) or "."
            out_stem = os.path.splitext(os.path.basename(output_file))[0]
            image_dir = os.path.join(out_dir, f"{out_stem}-images")
            image_rel_dir = f"{out_stem}-images"
            if out_dir != ".":
                image_rel_dir = f"{out_dir}/{out_stem}-images"
        else:
            image_dir = "feishu-images"
            image_rel_dir = "feishu-images"
        image_map = download_all_images(token, blocks, image_dir)
    else:
        print(f"[4/5] Skipping image download (--no-images)", file=sys.stderr)

    print(f"[5/5] Converting to Markdown...", file=sys.stderr)
    md = blocks_to_markdown(blocks, image_map, image_rel_dir)

    # Add frontmatter
    output = f"""---
title: "{title}"
source: "{source_url}"
fetched: "{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}"
type: "飞书文档"
---

# {title}

{md}
"""

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"✓ Saved to {output_file} ({len(output)} chars)", file=sys.stderr)
    else:
        print(output)

if __name__ == "__main__":
    main()
