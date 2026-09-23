import { IconButton, Select } from "@/components/ui";
import { LockIcon, LogOutIcon, MenuIcon, MoonIcon, SunIcon } from "@/components/ui/icons";
import { useTheme } from "@/app/ThemeContext";
import { describeWindow, useSignalWindow } from "@/app/SignalWindowContext";
import { useAuth } from "@/app/AuthContext";
import { Avatar } from "@/components/ui";
import { WorkspaceSwitcher } from "./WorkspaceSwitcher";
import styles from "./Topbar.module.css";

export interface TopbarProps {
  title: string;
  onMenuClick: () => void;
}

export function Topbar({ title, onMenuClick }: TopbarProps) {
  const { theme, toggle } = useTheme();
  const { windowDays, setWindowDays, options, userChoice, label } = useSignalWindow();
  const { logout, session } = useAuth();

  return (
    <header className={styles.topbar}>
      <div className={styles.left}>
        <span className={styles.menuBtn}>
          <IconButton label="Open menu" icon={<MenuIcon />} onClick={onMenuClick} />
        </span>
        <h1 className={styles.title}>{title}</h1>
        <WorkspaceSwitcher />
      </div>

      <div className={styles.right}>
        {userChoice ? (
          <span className={styles.signalWindow}>
            <Select
              aria-label="Signal window: how far back signals, Inbox tasks and alerts go"
              title={`Showing signals from ${describeWindow(windowDays)}`}
              value={windowDays === null ? "all" : String(windowDays)}
              onChange={(e) => {
                const v = e.target.value;
                setWindowDays(v === "all" ? null : Number(v));
              }}
              options={options.map((o) => ({
                value: o.days === null ? "all" : String(o.days),
                label: `Signals: ${o.label}`,
              }))}
            />
          </span>
        ) : (
          // A superadmin has set one window for everyone. Shown, not offered: a picker that
          // cannot change anything would look broken.
          <span
            className={`${styles.signalWindow} ${styles.windowFixed}`}
            title={`Set by your administrator: signals from ${describeWindow(windowDays)}`}
          >
            <LockIcon aria-hidden="true" className={styles.windowLock} />
            <span>Signals: {label}</span>
            <span className="sr-only">, set by your administrator</span>
          </span>
        )}
        <IconButton
          label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
          icon={theme === "dark" ? <SunIcon /> : <MoonIcon />}
          onClick={toggle}
        />
        <IconButton label="Sign out" icon={<LogOutIcon />} onClick={logout} />
        {session && <Avatar name={session.role} size="sm" />}
      </div>
    </header>
  );
}
