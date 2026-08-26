# Design Quality Checklist

Use this checklist when a UI looks visually inconsistent or after a frontend change with visible impact.

## System Discovery

- Identify design tokens: CSS variables, Tailwind theme, theme provider, Sass variables, token JSON, component variants.
- Identify component primitives: button, input, select, tabs, modal, card, table, badge, tooltip, icon button.
- Identify icon source: local icon wrapper, Lucide, Heroicons, Material, Font Awesome, react-icons, custom SVG folder.
- Identify typography source: global CSS, framework defaults, web-font imports, platform stacks, component overrides.

## Color Audit

- Count raw color literals and compare them to semantic tokens.
- Replace repeated literals with named tokens in the local system.
- Collapse near-duplicates when they represent the same role.
- Ensure status colors communicate state consistently: success, warning, danger, info, neutral.
- Check text contrast on buttons, badges, disabled controls, table cells, and colored backgrounds.

## Typography Audit

- Remove accidental font imports.
- Keep UI font stacks centralized.
- Normalize heading sizes and weights across comparable panels.
- Ensure labels, table text, controls, and helper text use consistent sizes and line heights.
- Avoid ultra-light text, cramped line height, and oversized headings inside dense tools.

## Icon Audit

- Prefer the existing icon wrapper or dominant library.
- Replace inconsistent icon libraries in the touched surface.
- Normalize icon size and stroke width.
- Use icon-only buttons only for common actions or when a tooltip/accessibility label exists.
- Do not use icons as generic decoration in forms or dashboards.

## Layout Audit

- Check desktop and mobile widths.
- Test long labels, Chinese strings, numbers, dates, and status pills.
- Ensure controls have stable dimensions in hover, loading, selected, disabled, and error states.
- Keep spacing on the project scale, usually multiples of 4 or 8.
- Avoid cards inside cards and repeated decorative containers.

## Final Visual QA

- Run app build/typecheck/lint if available.
- Inspect screenshots or browser render, not just code.
- Scan the CSS after edits for new raw colors or font-family declarations.
- Verify the page still feels like the same product, only cleaner.
