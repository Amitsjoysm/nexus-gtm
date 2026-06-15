import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { FormEvent } from "react";
import { Badge, Button, Field, Input, Modal, Skeleton, useToast } from "@/components/ui";
import { BuildingIcon, CheckIcon, ChevronRightIcon, PlusIcon } from "@/components/ui/icons";
import { useApiClient, useAuth } from "@/app/AuthContext";
import { useApi } from "@/hooks/useApi";
import { ApiError } from "@/lib/api";
import type { TenantSummary } from "@/lib/types";
import styles from "./WorkspaceSwitcher.module.css";

interface MenuRect {
  top: number;
  left: number;
  minWidth: number;
}

const MENU_MIN_WIDTH = 240;
const MENU_GAP = 6;
const VIEWPORT_PAD = 8;

/** name -> URL-safe slug matching the backend pattern (^[a-z0-9][a-z0-9-]{1,79}$). */
function slugify(name: string): string {
  return name
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
}

/**
 * App-shell control for switching between the workspaces (tenants) a user belongs to.
 * Always interactive: the menu lists every workspace the user owns/joins, plus a "Create
 * workspace" action — so even a brand-new single-workspace owner has a path to a second one
 * (a one-tenant user used to see dead static text with nothing to switch to). Portaled menu
 * (position:fixed) escapes the sticky topbar's backdrop-filter/overflow.
 */
export function WorkspaceSwitcher() {
  const api = useApiClient();
  const { session, switchTenant, createWorkspace } = useAuth();
  const toast = useToast();
  const { data: tenants, loading } = useApi((signal) => api.listTenants(signal), []);

  const currentId = session?.tenantId ?? null;
  const list = tenants ?? [];
  const current = list.find((t) => t.tenant_id === currentId) ?? null;
  const currentName = current?.name ?? "Workspace";

  if (loading) {
    return (
      <div className={styles.staticTrigger} aria-hidden="true">
        <span className={styles.icon}>
          <BuildingIcon />
        </span>
        <Skeleton width={96} height={14} />
      </div>
    );
  }

  return (
    <SwitcherMenu
      tenants={list}
      currentId={currentId}
      currentName={currentName}
      onSwitch={switchTenant}
      onCreate={createWorkspace}
      onToastSuccess={(name) => toast.success(`Switched to ${name}`)}
      onToastError={(message) => toast.error("Couldn't switch workspace", message)}
      onCreateSuccess={(name) => toast.success(`Created ${name}`, "You're now in the new workspace.")}
    />
  );
}

function SwitcherMenu({
  tenants,
  currentId,
  currentName,
  onSwitch,
  onCreate,
  onToastSuccess,
  onToastError,
  onCreateSuccess,
}: {
  tenants: TenantSummary[];
  currentId: string | null;
  currentName: string;
  onSwitch: (tenantId: string) => Promise<void>;
  onCreate: (body: { name: string; slug: string }) => Promise<void>;
  onToastSuccess: (name: string) => void;
  onToastError: (message: string) => void;
  onCreateSuccess: (name: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [switching, setSwitching] = useState(false);
  const [rect, setRect] = useState<MenuRect | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const [createOpen, setCreateOpen] = useState(false);

  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);

  // The "Create workspace" row is the last focusable item after the tenant list.
  const itemCount = tenants.length + 1;
  const createIndex = tenants.length;

  const currentIndex = Math.max(
    0,
    tenants.findIndex((t) => t.tenant_id === currentId),
  );

  const place = useCallback(() => {
    const node = triggerRef.current;
    if (!node) return;
    const r = node.getBoundingClientRect();
    const minWidth = Math.max(MENU_MIN_WIDTH, r.width);
    const left = Math.min(r.left, window.innerWidth - minWidth - VIEWPORT_PAD);
    setRect({ top: r.bottom + MENU_GAP, left: Math.max(VIEWPORT_PAD, left), minWidth });
  }, []);

  const openMenu = useCallback(() => {
    place();
    setActiveIndex(currentIndex);
    setOpen(true);
  }, [place, currentIndex]);

  const closeMenu = useCallback((restoreFocus = true) => {
    setOpen(false);
    if (restoreFocus) triggerRef.current?.focus();
  }, []);

  useLayoutEffect(() => {
    if (!open) return;
    place();
    const onResize = () => place();
    const onScroll = () => place();
    window.addEventListener("resize", onResize);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      window.removeEventListener("resize", onResize);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [open, place]);

  useEffect(() => {
    if (!open) return;
    itemRefs.current[activeIndex]?.focus();
  }, [open, activeIndex]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (menuRef.current?.contains(target) || triggerRef.current?.contains(target)) return;
      closeMenu(false);
    };
    document.addEventListener("pointerdown", onPointerDown, true);
    return () => document.removeEventListener("pointerdown", onPointerDown, true);
  }, [open, closeMenu]);

  const select = useCallback(
    async (tenant: TenantSummary) => {
      if (switching) return;
      if (tenant.tenant_id === currentId) {
        closeMenu();
        return;
      }
      setSwitching(true);
      try {
        await onSwitch(tenant.tenant_id);
        onToastSuccess(tenant.name);
        closeMenu(false);
      } catch (err) {
        onToastError(err instanceof Error ? err.message : "Please try again.");
      } finally {
        setSwitching(false);
      }
    },
    [switching, currentId, onSwitch, onToastSuccess, onToastError, closeMenu],
  );

  function onMenuKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        setActiveIndex((i) => (i + 1) % itemCount);
        break;
      case "ArrowUp":
        event.preventDefault();
        setActiveIndex((i) => (i - 1 + itemCount) % itemCount);
        break;
      case "Home":
        event.preventDefault();
        setActiveIndex(0);
        break;
      case "End":
        event.preventDefault();
        setActiveIndex(itemCount - 1);
        break;
      case "Escape":
        event.preventDefault();
        closeMenu();
        break;
      case "Tab":
        closeMenu(false);
        break;
      default:
        break;
    }
  }

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className={styles.trigger}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Current workspace: ${currentName}. Switch or create a workspace`}
        onClick={() => (open ? closeMenu() : openMenu())}
      >
        <span className={styles.icon} aria-hidden="true">
          <BuildingIcon />
        </span>
        <span className={styles.triggerName}>{currentName}</span>
        <span className={`${styles.caret} ${open ? styles.caretOpen : ""}`} aria-hidden="true">
          <ChevronRightIcon />
        </span>
      </button>

      {open &&
        rect &&
        createPortal(
          <div
            ref={menuRef}
            className={styles.menu}
            role="menu"
            aria-label="Switch workspace"
            aria-busy={switching}
            style={{ top: rect.top, left: rect.left, minWidth: rect.minWidth }}
            onKeyDown={onMenuKeyDown}
          >
            <p className={styles.menuHead} id="workspace-menu-head">
              Workspaces
            </p>
            <ul className={styles.list} aria-labelledby="workspace-menu-head">
              {tenants.map((tenant, index) => {
                const isCurrent = tenant.tenant_id === currentId;
                return (
                  <li key={tenant.tenant_id} role="none">
                    <button
                      ref={(el) => {
                        itemRefs.current[index] = el;
                      }}
                      type="button"
                      role="menuitem"
                      className={styles.item}
                      tabIndex={index === activeIndex ? 0 : -1}
                      aria-current={isCurrent ? "true" : undefined}
                      disabled={switching}
                      onClick={() => void select(tenant)}
                    >
                      <span className={styles.itemMain}>
                        <span className={styles.itemName}>{tenant.name}</span>
                        <Badge tone="neutral" className={styles.role}>
                          {tenant.role}
                        </Badge>
                      </span>
                      <span className={styles.check} aria-hidden={!isCurrent}>
                        {isCurrent && <CheckIcon />}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
            <div className={styles.footer}>
              <button
                ref={(el) => {
                  itemRefs.current[createIndex] = el;
                }}
                type="button"
                role="menuitem"
                className={styles.createItem}
                tabIndex={createIndex === activeIndex ? 0 : -1}
                disabled={switching}
                onClick={() => {
                  closeMenu(false);
                  setCreateOpen(true);
                }}
              >
                <span className={styles.createIcon} aria-hidden="true">
                  <PlusIcon />
                </span>
                Create workspace
              </button>
            </div>
          </div>,
          document.body,
        )}

      {createOpen && (
        <CreateWorkspaceModal
          onClose={() => setCreateOpen(false)}
          onCreate={onCreate}
          onCreated={(name) => {
            setCreateOpen(false);
            onCreateSuccess(name);
          }}
        />
      )}
    </>
  );
}

function CreateWorkspaceModal({
  onClose,
  onCreate,
  onCreated,
}: {
  onClose: () => void;
  onCreate: (body: { name: string; slug: string }) => Promise<void>;
  onCreated: (name: string) => void;
}) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Auto-derive the slug from the name until the user edits it directly.
  const effectiveSlug = slugEdited ? slug : slugify(name);
  const slugValid = /^[a-z0-9][a-z0-9-]{1,79}$/.test(effectiveSlug);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!name.trim() || !slugValid) return;
    setSubmitting(true);
    setError(null);
    try {
      await onCreate({ name: name.trim(), slug: effectiveSlug });
      onCreated(name.trim());
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Couldn't create the workspace.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title="Create a workspace"
      description="A new, separate workspace with its own accounts, signals, and members. You'll switch into it right away."
      size="sm"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button
            form="create-workspace-form"
            type="submit"
            loading={submitting}
            disabled={!name.trim() || !slugValid}
          >
            Create workspace
          </Button>
        </>
      }
    >
      <form id="create-workspace-form" onSubmit={onSubmit} noValidate>
        <Field label="Workspace name" required>
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Acme West Region"
            autoFocus
            required
          />
        </Field>
        <Field
          label="Workspace URL"
          hint="Lowercase letters, numbers, and hyphens. Used to identify the workspace."
          error={!slugValid && effectiveSlug.length > 0 ? "At least 2 chars: a-z, 0-9, hyphens." : error ?? undefined}
        >
          <Input
            value={effectiveSlug}
            onChange={(e) => {
              setSlugEdited(true);
              setSlug(slugify(e.target.value));
            }}
            placeholder="acme-west"
          />
        </Field>
      </form>
    </Modal>
  );
}
