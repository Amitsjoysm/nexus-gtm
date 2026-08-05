import { NavLink } from "react-router-dom";
import { cn } from "@/lib/cn";
import { NAV_ITEMS, canSee, navState } from "@/app/nav";
import { usePlatformIdentity } from "@/app/RequirePlatformAdmin";
import { useEntitlements, isLocked } from "@/app/EntitlementsContext";
import { useAuth } from "@/app/AuthContext";
import { Icons } from "@/components/ui";
import styles from "./Sidebar.module.css";

export interface SidebarProps {
  /** Mobile drawer open state. */
  open: boolean;
  /** Desktop icons-only collapse. */
  collapsed?: boolean;
  onNavigate: () => void;
  onToggleCollapse?: () => void;
}

export function Sidebar({ open, collapsed = false, onNavigate, onToggleCollapse }: SidebarProps) {
  const { session } = useAuth();
  const role = session?.role;
  // Platform admin is orthogonal to the workspace role, so it is read from its own source. Null
  // for every ordinary member, which is what hides the staff entry.
  const isPlatformAdmin = Boolean(usePlatformIdentity()?.is_platform_admin);
  // Null until loaded, and null on error — `isLocked` reads that as "nothing is locked", so a
  // slow or failing billing endpoint never deletes the customer's navigation.
  const entitlements = useEntitlements();

  return (
    <aside
      className={cn(styles.sidebar, open && styles.open, collapsed && styles.collapsed)}
      aria-label="Primary"
    >
      <div className={styles.header}>
        {onToggleCollapse && (
          <button
            type="button"
            className={styles.hamburger}
            onClick={onToggleCollapse}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-pressed={collapsed}
            title={collapsed ? "Expand menu" : "Collapse menu"}
          >
            <Icons.MenuIcon />
          </button>
        )}
        <div className={styles.brand}>
          <img src="/infojoy-mark.png" alt="Infojoy" className={styles.logoImg} />
          <span className={styles.brandText}>
            <span className={styles.brandInfo}>Info</span>
            <span className={styles.brandJoy}>Joy</span>{" "}
            <span className={styles.brandSub}>GTM</span>
          </span>
        </div>
      </div>

      <nav className={styles.nav} aria-label="Main navigation">
        {NAV_ITEMS.filter((item) => canSee(item, role, isPlatformAdmin)).map((item) => {
          const state = navState(role, isLocked(entitlements, item.capability));
          if (state === "hidden") return null;

          if (state === "locked") {
            // Routed to Billing rather than to the feature: the item is an upsell, and sending
            // someone to a page the server will 402 is the dead end this whole change exists to
            // remove. `aria-describedby` (not the label) carries the reason, so a screen reader
            // announces "Network, not included in your plan" instead of a bare link.
            return (
              <NavLink
                key={item.to}
                to="/settings/billing"
                onClick={onNavigate}
                title={`${item.label} is not included in your plan — view upgrade options`}
                className={cn(styles.link, styles.locked)}
                aria-describedby={`nav-lock-${item.to.replace(/\W/g, "")}`}
              >
                <span className={styles.icon} aria-hidden="true">
                  {item.icon}
                </span>
                <span className={styles.linkLabel}>{item.label}</span>
                <span className={styles.lockBadge} aria-hidden="true">
                  <Icons.LockIcon />
                </span>
                <span
                  id={`nav-lock-${item.to.replace(/\W/g, "")}`}
                  className={styles.srOnly}
                >
                  not included in your plan
                </span>
              </NavLink>
            );
          }

          return (
            <NavLink
              key={item.to}
              to={item.to}
              onClick={onNavigate}
              title={collapsed ? item.label : undefined}
              className={({ isActive }) => cn(styles.link, isActive && styles.active)}
            >
              <span className={styles.icon} aria-hidden="true">
                {item.icon}
              </span>
              <span className={styles.linkLabel}>{item.label}</span>
            </NavLink>
          );
        })}
      </nav>

      <div className={styles.footer}>
        <span className={styles.badge}>{role ?? "—"}</span>
        <span className={styles.footText}>Signed in</span>
      </div>
    </aside>
  );
}
