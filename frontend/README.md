# NEXUS GTM — Frontend

Production React + TypeScript + Vite frontend for NEXUS GTM. Custom, accessible component
library (no UI kit), CSS-module design tokens, and a strict four-state data contract on every
screen. The build emits a static bundle to `../nexus/web/dist/`, which FastAPI serves with SPA
fallback.

```bash
npm install        # install deps (react, react-dom, react-router-dom, framer-motion)
npm run dev        # Vite dev server; proxies /api -> http://127.0.0.1:8000
npm run typecheck  # tsc -b, no emit
npm run build      # tsc -b && vite build -> ../nexus/web/dist
npm run preview    # serve the production build locally
```

---

## Component architecture

Layered, bottom-up. Each layer only depends on the ones below it, so any piece can be
understood and changed in isolation.

```
src/
  styles/            tokens.css (design tokens) + global.css (reset, focus, a11y)
  lib/               framework-free helpers
    cn.ts            className joiner
    format.ts        formatNumber / formatPercent / timeAgo / humanize / initials
    motion.ts        framer-motion presets (reduced-motion safe)
    types.ts         TS mirrors of the backend Pydantic schemas (the API contract)
    api.ts           ApiClient + ApiError (typed fetch, AbortSignal, 401 handling)
    display.ts       domain value -> UI tone/label mappings
  components/
    ui/              reusable primitives (Button, Input, Card, Badge, Modal, DataTable, …)
    layout/          AppShell, Sidebar, Topbar, PageHeader
    DataState.tsx    enforces loading/error/empty/success for one data source
    StatCard.tsx     composed KPI card
  hooks/
    useApi.ts        data fetching with cancellation, error state, refetch
  app/
    ThemeContext     dark/light theme (persisted, prefers-color-scheme aware)
    AuthContext      session, ApiClient wiring, useAuth() / useApiClient()
    nav.tsx          nav model + role-based visibility
  pages/             one screen per route, composed from primitives
  App.tsx            providers + router + auth guards
```

**Design principles**

- **No hard-coded colors or spacing.** Everything references a token in `tokens.css`. Theming
  is a single `[data-theme]` attribute swap.
- **Primitives are dumb and reusable; pages are smart.** A `Button` knows nothing about the
  API; a page wires `useApi` + `ApiClient` and passes data down.
- **Accessibility is built into the primitives**, not bolted onto screens: real semantic
  elements, `:focus-visible` rings, labelled icon buttons, focus-trapped modals, roving-tabindex
  tabs, ≥44px targets, `prefers-reduced-motion`.
- **One data source = one `<DataState>`.** Loading/error/empty/success are never hand-rolled.

---

## Props / API design

Conventions shared across the library:

- Primitives extend the native element's props (`extends ButtonHTMLAttributes<…>`), so
  `onClick`, `disabled`, `aria-*`, `ref` etc. all work as expected.
- Variants are closed string unions (`variant="primary" | "secondary" | …`), never booleans
  that can combine into invalid states.
- Inputs are uncontrolled-friendly and inherit `id` / `aria-describedby` / `aria-invalid` from a
  wrapping `<Field>` via context — no manual id wiring at the call site.

### Data layer

```ts
// useApi: fetch with cancellation, error capture, and refetch.
const accounts = useApi<Account[]>((signal) => api.listAccounts(signal), []);
// -> { data, error, loading, refetch, setData }
```

```tsx
// DataState: the four-state contract for one source.
<DataState
  state={accounts}
  skeleton={<TableSkeleton />}              // first load
  isEmpty={(rows) => rows.length === 0}
  empty={<EmptyState title="No accounts yet" … />}
  errorTitle="Couldn't load accounts"        // error -> ErrorState + Retry
>
  {(rows) => <AccountsTable rows={rows} />}   // success
</DataState>
```

`ApiClient` returns typed promises and throws `ApiError(status, detail)` carrying the backend
`detail` string, so error states show real messages. A `401` triggers `onUnauthorized`, which
the `AuthProvider` uses to clear the session and route back to login.

### Representative primitive APIs

| Component   | Key props |
|-------------|-----------|
| `Button`    | `variant`, `size`, `loading`, `fullWidth`, `iconLeft/Right` + native button props |
| `IconButton`| `label` (required, a11y), `icon`, `variant`, `size` |
| `Field`     | `label`, `hint`, `error`, `required`, `hideLabel` — wraps one control |
| `Input/Textarea/Select` | native props; pick up id/aria from `Field`; `invalid` |
| `Card`      | `interactive`, `padding="none"|"sm"|"md"|"lg"` |
| `Badge`     | `tone`, `dot`, `icon` |
| `Modal`     | `open`, `onClose`, `title`, `description`, `footer`, `size` (focus-trapped) |
| `DataTable<T>` | `columns: Column<T>[]`, `rows`, `getRowKey`, `loading`, `empty`, `onRowClick` |
| `Tabs`      | `items`, `value`, `onChange` (roving arrow-key tablist) + `TabPanel` |
| `useToast()`| `toast()`, `success()`, `error()` from `ToastProvider` |

---

## Usage example

```tsx
import { PageHeader } from "@/components/layout/PageHeader";
import { Button, Card, EmptyState, Icons, useToast } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { InboxTask } from "@/lib/types";

export function InboxExample() {
  const api = useApiClient();
  const toast = useToast();
  const inbox = useApi<InboxTask[]>((signal) => api.listInbox(signal), []);

  async function complete(task: InboxTask) {
    await api.completeTask(task.id);
    inbox.setData((prev) => (prev ?? []).filter((t) => t.id !== task.id)); // optimistic
    toast.success("Task completed", task.title);
  }

  return (
    <>
      <PageHeader title="Inbox" actions={<Button onClick={inbox.refetch}>Refresh</Button>} />
      <DataState
        state={inbox}
        skeleton={<Card padding="md">…</Card>}
        isEmpty={(rows) => rows.length === 0}
        empty={<EmptyState icon={<Icons.InboxIcon />} title="Inbox zero" />}
      >
        {(rows) =>
          rows.map((t) => (
            <Card key={t.id} padding="md">
              {t.title}
              <Button variant="secondary" onClick={() => complete(t)}>Complete</Button>
            </Card>
          ))
        }
      </DataState>
    </>
  );
}
```

---

## Best practices (followed throughout)

- **Every data view handles all four states.** Skeletons mirror the real layout shape rather
  than showing a bare spinner; empty states always offer a way forward.
- **Cancel in-flight requests.** `useApi` aborts on dep change/unmount to prevent races and
  setState-after-unmount.
- **Optimistic mutations with `setData`,** reconciled by a `refetch` or rolled back on error
  (with a toast).
- **Color is never the only signal** — badges pair tone with text and a dot.
- **Keyboard + screen-reader first:** focus rings, focus trapping, labelled controls, semantic
  landmarks, and `aria-live` toasts.
- **Reduced motion respected** everywhere via `useReducedMotion` and shared `lib/motion` presets.
- **Strict TypeScript** (`noUnusedLocals/Parameters`, `strict`) and a typed API contract that
  mirrors the backend schemas in `lib/types.ts`.
- **Reduce external dependencies:** four runtime packages, a hand-rolled component library, and
  SVG icons in `components/ui/icons.tsx` — no icon font, no CSS framework.

---

## Routes

`/login` (public) · `/dashboard` · `/inbox` · `/accounts` · `/accounts/:id` · `/signals` ·
`/alerts` · `/members` (manager+). Unknown paths redirect to `/dashboard`. On a hard refresh of
any deep link, FastAPI's `SPAStaticFiles` returns `index.html` so the client router resolves it.
