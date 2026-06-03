import { IconButton } from "@/components/ui";
import { LogOutIcon, MenuIcon, MoonIcon, SunIcon } from "@/components/ui/icons";
import { useTheme } from "@/app/ThemeContext";
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
