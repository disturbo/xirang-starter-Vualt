---
name: taste-skill
description: UI taste and visual consistency guardrail for frontend coding. Use when Codex builds, edits, reviews, or polishes web/app UI, HTML/CSS prototypes, React/Vue/Svelte/Tailwind screens, dashboards, forms, design systems, or visual components; especially when the user mentions taste, polish, colors, fonts, typography, icons, design consistency, visual QA, "looks messy", or asks to prevent random palettes, font sprawl, or icon misuse.
---

# Taste Skill

## Purpose

Make UI work coherent, restrained, and product-appropriate. Treat taste as a repeatable engineering constraint: discover the existing system, reduce visual entropy, implement with tokens/components, and verify in the browser when a frontend can run.

This skill does not replace product requirements or brand guidelines. Existing project conventions, design tokens, component libraries, and user-provided designs always win.

## Workflow

1. Discover the local design system before editing:
   - Read `AGENTS.md`, `README`, package metadata, app entry CSS, theme files, Tailwind config, design-token files, and common component folders.
   - Identify the dominant icon library, typography stack, spacing scale, radii, elevation model, and semantic colors.
   - If no system exists, create the smallest useful token layer in the existing styling mechanism instead of scattering literals.

2. Run a quick visual audit when the project has inspectable frontend files:
   ```bash
   python3 ~/.codex/skills/taste-skill/scripts/visual_audit.py <project-or-file>
   ```
   Use the output to find raw colors, font-family sprawl, icon-library mixing, font-size drift, and radius drift. The audit is advisory; inspect context before changing code.

3. Constrain the visual vocabulary:
   - Colors: use semantic tokens or existing CSS variables/classes. Avoid new raw hex/rgb/hsl literals unless adding a named token.
   - Typography: use one primary font stack; allow one mono or display stack only when already established or domain-justified.
   - Icons: use the existing project icon system. If none exists and an icon package is already acceptable, prefer one library consistently.
   - Shape and spacing: keep radii, borders, shadows, and spacing on the discovered scale.

4. Implement with the smallest coherent change:
   - Replace one-off visual literals with tokens/classes.
   - Consolidate duplicate font imports and stray `font-family` declarations.
   - Replace mismatched icon sets with the dominant library or local icon component.
   - Do not do broad visual rewrites unrelated to the requested screen or component.

5. Verify:
   - Run the repo's formatter, typecheck, tests, or build when available.
   - For frontend changes, open the running page in a browser and inspect desktop and mobile widths when feasible.
   - Check that text does not overflow or overlap, buttons keep stable dimensions, icons align optically, contrast is adequate, and the result fits the product domain.

## Taste Rules

### Colors

- Prefer semantic names such as `surface`, `border`, `muted`, `primary`, `success`, `warning`, and `danger`.
- Keep neutral surfaces mostly neutral; use accent colors for meaning or action, not decoration.
- Do not create several near-identical blues, grays, reds, or brand accents in one feature.
- Avoid one-note palettes dominated by a single hue family unless the established brand requires it.
- Preserve accessible contrast for text, controls, charts, and status badges.

### Typography

- Keep font families few and deliberate. More than two UI families is usually a bug.
- Do not use viewport-width font scaling for normal UI. Use stable, responsive type scales.
- Keep letter spacing at `0` unless the existing design system intentionally uses it.
- Match type size to container density: dashboards, forms, sidebars, and tables need compact hierarchy, not hero-scale headings.
- Prefer weight, size, and color tokens over ad hoc `font-weight`, `font-size`, and `line-height` literals.

### Icons

- Use one icon language per surface. Mixing Lucide, Heroicons, Font Awesome, Material Icons, and hand-written SVGs in the same feature should be treated as visual debt.
- Put icons in buttons where a recognizable symbol exists; add accessible labels or tooltips for icon-only actions.
- Keep stroke width, optical size, and alignment consistent.
- Avoid decorative icons that do not help recognition, scanning, or action.

### Layout

- Use predictable grids, stable control dimensions, and responsive constraints.
- Do not nest cards inside cards or turn ordinary page sections into decorative floating cards.
- Keep operational tools dense, scannable, and calm; reserve expressive visuals for consumer, editorial, brand, or game experiences.
- Check long Chinese and English strings, numbers, status labels, and empty states at narrow widths.

## When Auditing Existing UI

Use the audit script first, then inspect the most relevant files. Prioritize fixes in this order:

1. Visual bugs that affect usability: overlap, low contrast, tiny hit targets, unstable layout, missing states.
2. System breaks: random fonts, multiple icon libraries, raw color literals beside tokens.
3. Polish issues: inconsistent radii, shadows, spacing, heading scale, badge styles.
4. Nice-to-have refinements: animation, microcopy, decorative details.

For a deeper manual checklist, read `references/design-quality-checklist.md`.

## Reporting

When finishing a UI task, briefly state:

- The visual system or dominant conventions used.
- The colors/fonts/icons consolidated.
- The verification performed and any remaining visual risk.
