---
name: generate-diagram
description: Choose and create a maintainable diagram for a knowledge-base note, including Mermaid, SVG, or a native editable diagram format. Use when relationships, sequence, architecture, state, or hierarchy are materially clearer visually than in prose.
---

# Generate Diagram

Choose the smallest maintainable format that explains the relationship:

- Mermaid for compact flows, state diagrams, and simple relationships.
- SVG for precise static architecture or publication-quality visuals.
- A native editable diagram or whiteboard format for large collaborative flows, swimlanes, or frequent editing.

Before drawing, identify the diagram's question, audience, source facts, node semantics, edge semantics, and expected edit lifecycle. Do not invent process steps to make the diagram look complete.

Before implementation, read `30-规范/流程图绘制规范.md`; for SVG architecture also read `30-规范/SVG架构图设计规范.md`. If the source is a meeting artifact, also read `30-规范/会议纪要整理规范.md` and preserve the source/provenance relationship.

For a formal Feishu/Lark whiteboard flowchart, use native `table/frame + composite_shape + connector`; Mermaid, SVG, images, groups, scattered text shapes and independent grid lines are not a final delivery. Read back raw nodes after writing and verify attached endpoints, orthogonal routing, zero business crossings and the object-type constraints required by the flowchart standard, then export a visual preview.

Use consistent direction, spacing, color semantics, and labels. Avoid line crossings, detached connectors, decorative boxes, and text too small to read. Provide a text explanation or table when the diagram carries essential meaning.

Store editable source with the rendered result when they differ. Verify syntax or rendering, dimensions, labels, and correspondence with the surrounding document before delivery.
