# NEXUS GTM — Design System

A calm, dense, trustworthy product UI. Dark-first (the rep stares at it all day), with a
fully tokenized light theme. Follows ui-ux-pro-max accessibility rules (contrast ≥ 4.5:1,
visible focus, full keyboard nav, ≥ 44×44px targets) and impeccable craft (committed choices,
real states, no placeholder mush). Motion via framer-motion, always reduced-motion-safe.

## Design tokens
Single source of truth: `frontend/src/styles/tokens.css` (CSS custom properties).
Components consume tokens only — no hard-coded colors/spacing.

### Color (semantic, themeable via `[data-theme]`)
- Surfaces: `--bg`, `--surface`, `--surface-2`, `--surface-3`, `--overlay`
- Lines: `--border`, `--border-strong`
- Text: `--text`, `--text-muted`, `--text-subtle`, `--text-inverse`
- Brand: `--accent`, `--accent-hover`, `--accent-quiet`, `--accent-contrast`
- Status: `--success`, `--warning`, `--danger`, `--info` (+ `-quiet` tinted backgrounds)
- Focus: `--ring` (2px outline, 2px offset, always visible on keyboard focus)

Contrast: body text on surfaces ≥ 4.5:1; muted text ≥ 4.5:1 on its surface; large/secondary ≥ 3:1.

### Typography
- Family: system UI stack; `--font-mono` for codes/ids/metrics.
- Scale (1.20 minor third): `--text-xs` 12, `--text-sm` 13, `--text-base` 14,
  `--text-md` 16, `--text-lg` 20, `--text-xl` 24, `--text-2xl` 30, `--text-3xl` 36.
- Weights: 400 / 500 / 600 / 700. Line-heights: `--leading-tight` 1.2, `--leading` 1.5.

### Spacing (4px base)
`--space-1`=4 … `--space-2`=8, `-3`=12, `-4`=16, `-5`=20, `-6`=24, `-8`=32, `-10`=40, `-12`=48.

### Radii / shadow / motion
- Radii: `--radius-sm` 6, `--radius` 10, `--radius-lg` 14, `--radius-full` 999.
- Elevation: `--shadow-sm`, `--shadow`, `--shadow-lg` (soft, low-spread; subtle in dark).
- Motion: durations `--dur-fast` 120ms, `--dur` 180ms, `--dur-slow` 260ms;
  easings `--ease-out`, `--ease-in-out`, spring presets in `lib/motion.ts`.
  All motion gated by `prefers-reduced-motion`.

### Layout
- Breakpoints: `sm` 640, `md` 768, `lg` 1024, `xl` 1280.
- App shell: fixed left Sidebar (collapses to a drawer < md), sticky Topbar, scrollable content.
- Content max-width `--container` 1200px for reading comfort; tables go full width.

## Component library (`frontend/src/components/ui/`)
Primitives: Button, IconButton, Input, Textarea, Select, Field (label+hint+error),
Card, Badge, Tag, Avatar, Spinner, Skeleton, EmptyState, ErrorState, Modal/Dialog,
Toast (+provider), Table, Tabs, Tooltip, Menu. Each: typed props, `forwardRef`,
accessible roles/labels, and explicit disabled/loading variants.

### State contract (every data view)
1. **loading** → skeletons matching final layout (never a bare spinner for page loads).
2. **empty** → `EmptyState` with icon, headline, one-line guidance, and a primary action.
3. **error** → `ErrorState` with the message and a Retry.
4. **success** → the data.
`useApi`/`DataState` enforce this so screens can't forget a state.

## Accessibility checklist (enforced)
- Semantic landmarks: `header`, `nav`, `main`. One `h1` per screen, ordered headings.
- All interactive elements reachable by keyboard; visible `--ring` focus; Esc closes overlays;
  focus trap + restore in Modal.
- Labels for every control (`<label>`/`aria-label`); errors via `aria-describedby`,
  `aria-invalid`. Live regions for toasts (`role="status"`) and async results.
- Touch targets ≥ 44×44px. Color never the sole signal (icon/text accompany status colors).
- Respect `prefers-reduced-motion` and `prefers-color-scheme`.

## Muted text meets AA (measured, 2026-08-25)

`--text-subtle` shipped at **3.09:1 on white** in the light theme and 3.90:1 on `--surface-2` in the
dark one, against the 4.5:1 floor this document calls non-negotiable. It is the colour of every
`Field` hint, and a hint carries the one sentence explaining what a control expects — the last text
in the product that should render as decoration. `--text-muted` was also 4.49:1 on light
`--surface-2`: one hundredth under, which is still under.

Both are now solved against the **lightest surface each actually lands on**, not against the body
background:

| Token | Was | Now | Worst-case ratio |
|---|---|---|---|
| `--text-subtle` (light) | `#8494ab` | `#64748b` | 4.50:1 on `--surface-2` |
| `--text-subtle` (dark) | `#7278a8` | `#858bbb` | 4.54:1 on `--surface-3` |
| `--text-muted` (light) | `#64748d` | `#62728b` | 4.62:1 on `--surface-2` |

Verified in the running app across 110 text nodes per theme: zero failures in either.

**When auditing contrast in a browser, do not toggle the theme with JavaScript while the pane
emulates the other scheme.** Doing that produced 20 phantom failures — light-theme values computed
against a dark background, including a 1.14:1 reading — none of them real. Set the emulated scheme,
reload, then measure.
