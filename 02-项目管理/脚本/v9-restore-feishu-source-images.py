#!/usr/bin/env python3
"""按旧快照与飞书 token 快照的文本锚点恢复缺失图片。"""

from __future__ import annotations

import argparse
import concurrent.futures
import difflib
import hashlib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "20-资料" / "业务文件" / "09-飞书源文档"
OLD_SNAPSHOT = SOURCE_ROOT / "示例项目服务模块PRD-飞书源文档-旧版0516.md"
TOKEN_SNAPSHOT = SOURCE_ROOT / "示例项目服务模块PRD-飞书源文档.md"
TARGET_DIR = ROOT / "20-资料" / "业务文件" / "示例项目服务模块PRD-飞书源文档-images"
MANIFEST = TARGET_DIR / "source-map.json"
OLD_IMAGE_RE = re.compile(r"!\[\[.*?/img_(\d{3})\.(png|jpg)\]\]")
TOKEN_IMAGE_RE = re.compile(r"!\[\]\(https://feishu\.cn/file/([^)]+)\)")
EXPECTED_UNRESOLVED = [145, 146, 147]


def normalized_text(line: str) -> str:
    return re.sub(r"\s+", "", line).strip().lower()


def text_anchors(lines: list[str], image_re: re.Pattern[str]) -> list[tuple[int, str]]:
    return [
        (index, normalized_text(line))
        for index, line in enumerate(lines)
        if normalized_text(line) and not image_re.search(line)
    ]


def build_mapping() -> tuple[dict[int, dict[str, str]], list[int], float]:
    old_lines = OLD_SNAPSHOT.read_text(encoding="utf-8").splitlines()
    token_lines = TOKEN_SNAPSHOT.read_text(encoding="utf-8").splitlines()
    old_anchors = text_anchors(old_lines, OLD_IMAGE_RE)
    token_anchors = text_anchors(token_lines, TOKEN_IMAGE_RE)
    matcher = difflib.SequenceMatcher(
        None,
        [item[1] for item in old_anchors],
        [item[1] for item in token_anchors],
        autojunk=False,
    )

    matched_lines = [(-1, -1)]
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            matched_lines.append(
                (old_anchors[block.a + offset][0], token_anchors[block.b + offset][0])
            )
    matched_lines.append((len(old_lines), len(token_lines)))

    mapping: dict[int, dict[str, str]] = {}
    for (old_start, token_start), (old_end, token_end) in zip(matched_lines, matched_lines[1:]):
        old_images = [
            match
            for line in old_lines[old_start + 1 : old_end]
            if (match := OLD_IMAGE_RE.search(line))
        ]
        token_images = [
            match
            for line in token_lines[token_start + 1 : token_end]
            if (match := TOKEN_IMAGE_RE.search(line))
        ]
        if not old_images or len(old_images) != len(token_images):
            continue
        for old_match, token_match in zip(old_images, token_images):
            number = int(old_match.group(1))
            candidate = {
                "filename": f"img_{number:03d}.{old_match.group(2)}",
                "token": token_match.group(1),
                "method": "equal-count-between-matched-text-anchors",
            }
            if number in mapping and mapping[number] != candidate:
                raise RuntimeError(f"图片 {number} 出现冲突映射")
            mapping[number] = candidate

    all_old_numbers = {
        int(match.group(1))
        for line in old_lines
        if (match := OLD_IMAGE_RE.search(line))
    }
    unresolved = sorted(all_old_numbers - set(mapping))
    if len(mapping) != 257 or unresolved != EXPECTED_UNRESOLVED:
        raise RuntimeError(
            f"映射结果漂移：mapped={len(mapping)}, unresolved={unresolved}"
        )
    if len({item["token"] for item in mapping.values()}) != len(mapping):
        raise RuntimeError("多个旧图片被映射到同一飞书 token")
    return mapping, unresolved, matcher.ratio()


def media_kind(path: Path) -> str:
    header = path.read_bytes()[:12]
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "webp"
    return "unknown"


def download_one(item: tuple[int, dict[str, str]], overwrite: bool) -> dict[str, object]:
    number, record = item
    output = TARGET_DIR / record["filename"]
    if output.is_file() and output.stat().st_size > 0 and not overwrite:
        status = "existing"
    else:
        relative_output = output.relative_to(ROOT)
        command = [
            "lark-cli",
            "docs",
            "+media-download",
            "--as",
            "user",
            "--token",
            record["token"],
            "--output",
            str(relative_output),
            "--format",
            "json",
        ]
        if overwrite:
            command.append("--overwrite")
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"img_{number:03d} 下载输出不可解析：{result.stdout[-200:]} {result.stderr[-200:]}"
            ) from exc
        if result.returncode != 0 or not payload.get("ok"):
            raise RuntimeError(f"img_{number:03d} 下载失败：{payload.get('error')}")
        status = "downloaded"
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError(f"img_{number:03d} 下载后文件缺失或为空")
    return {
        "number": number,
        **record,
        "status": status,
        "bytes": output.stat().st_size,
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "media_kind": media_kind(output),
    }


def write_manifest(
    records: list[dict[str, object]], unresolved: list[int], match_ratio: float
) -> None:
    payload = {
        "schema_version": "1.0",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_old_snapshot": str(OLD_SNAPSHOT.relative_to(ROOT)),
        "source_token_snapshot": str(TOKEN_SNAPSHOT.relative_to(ROOT)),
        "mapping_method": "equal-count image pairing between difflib-matched text anchors",
        "text_match_ratio": match_ratio,
        "mapped_count": len(records),
        "unresolved_numbers": unresolved,
        "unresolved_reason": "旧版三张 PDI 原型已被后续文档替换，当前 token 快照无等价资源；禁止猜测映射",
        "records": sorted(records, key=lambda item: int(item["number"])),
    }
    MANIFEST.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--download", action="store_true", help="下载 257 张已确定映射的图片")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在图片")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    if not 1 <= args.workers <= 8:
        parser.error("--workers 必须在 1..8")

    mapping, unresolved, ratio = build_mapping()
    print(json.dumps({"mapped": len(mapping), "unresolved": unresolved, "text_match_ratio": ratio}, ensure_ascii=False))
    if not args.download:
        return 0

    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    errors: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(download_one, item, args.overwrite): item[0]
            for item in sorted(mapping.items())
        }
        for future in concurrent.futures.as_completed(futures):
            number = futures[future]
            try:
                record = future.result()
                records.append(record)
                print(f"ok {number:03d} {record['bytes']} {record['media_kind']}", flush=True)
            except Exception as exc:  # 汇总失败，保留已成功的可重入结果
                errors.append(str(exc))
                print(f"error {number:03d} {exc}", flush=True)

    write_manifest(records, unresolved, ratio)
    summary = {"restored": len(records), "failed": len(errors), "unresolved": unresolved}
    print(json.dumps(summary, ensure_ascii=False))
    if errors:
        print(json.dumps({"errors": errors}, ensure_ascii=False, indent=2))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
