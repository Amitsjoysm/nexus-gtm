import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Badge, Skeleton, useToast } from "@/components/ui";
import { BuildingIcon, CheckIcon, ChevronRightIcon } from "@/components/ui/icons";
import { useApiClient, useAuth } from "@/app/AuthContext";
import { useApi } from "@/hooks/useApi";
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

/**
 * App-shell control for switching between the workspaces (tenants) a user belongs to.
 * Quiet by default: a labelled trigger showing the current workspace; a portaled menu
 * (position:fixed, so it escapes the sticky topbar's backdrop-filter/overflow) lists the
 * others. With one workspace it collapses to static text — no affordance the user can't use.
 */
export function WorkspaceSwitcher() {
  const api = useApiClient();
  const { session, switchTenant } = useAuth();
  const toast = useToast();
  const { data: tenants, loading } = useApi((signal) => api.listTenants(signal), []);

  const currentId = session?.tenantId ?? null;
  const current = tenants?.find((t) => t.tenant_id === currentId) ?? null;
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

  // One workspace (or the list failed to load): nothing to switch to — show it as plain text.
  if (!tenants || tenants.length <= 1) {
    return (
      <div className={styles.staticTrigger}>
        <span className={styles.icon} aria-hidden="true">
          <BuildingIcon />
        </span>
        <span className={styles.staticName}>{currentName}</span>
      </div>
    );
  }

  return (
    <SwitcherMenu
      tenants={tenants}
      currentId={currentId}
      currentName={currentName}
      onSwitch={switchTenant}
      onToastSuccess={(name) => toast.success(`Switched to ${name}`)}
      onToastError={(message) => toast.error("Couldn't switch workspace", message)}
    />
  );
}

function SwitcherMenu({
  tenants,
  currentId,
  currentName,
  onSwitch,
  onToastSuccess,
  onToastError,
}: {
  tenants: TenantSummary[];
  currentId: string | null;
  currentName: string;
  onSwitch: (tenantId: string) => Promise<void>;
  onToastSuccess: (name: string) => void;
  onToastError: (message: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [switching, setSwitching] = useState(false);
  const [rect, setRect] = useState<MenuRect | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);

  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);

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

  // Keep the menu anchored to the trigger while the viewport changes.
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

  // Move DOM focus to the active item as the user arrows through the menu.
  useEffect(() => {
    if (!open) return;
    itemRefs.current[activeIndex]?.focus();
  }, [open, activeIndex]);

  // Dismiss on outside pointer-down.
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
        setActiveIndex((i) => (i + 1) % tenants.length);
        break;
      case "ArrowUp":
        event.preventDefault();
        setActiveIndex((i) => (i - 1 + tenants.length) % tenants.length);
        break;
      case "Home":
        event.preventDefault();
        setActiveIndex(0);
        break;
      case "End":
        event.preventDefault();
        setActiveIndex(tenants.length - 1);
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
        aria-label={`Current workspace: ${currentName}. Switch workspace`}
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
          </div>,
          document.body,
        )}
    </>
  );
}
