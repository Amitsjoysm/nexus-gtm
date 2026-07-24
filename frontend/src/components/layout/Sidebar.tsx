import { NavLink } from "react-router-dom";
import { cn } from "@/lib/cn";
import { NAV_ITEMS, canSee } from "@/app/nav";
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
        {NAV_ITEMS.filter((item) => canSee(item, role)).map((item) => (
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
        ))}
      </nav>

      <div className={styles.footer}>
        <span className={styles.badge}>{role ?? "—"}</span>
        <span className={styles.footText}>Signed in</span>
      </div>
    </aside>
  );
}
