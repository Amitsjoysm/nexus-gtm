import { cn } from "@/lib/cn";
import { initials } from "@/lib/format";
import styles from "./Avatar.module.css";

export interface AvatarProps {
  name: string;
  size?: "sm" | "md" | "lg";
  /** Deterministic background hue derived from the name. */
  className?: string;
}

/** Initials avatar with a stable color per name. Decorative; name is announced elsewhere. */
export function Avatar({ name, size = "md", className }: AvatarProps) {
  const hue = hashHue(name);
  return (
    <span
      className={cn(styles.avatar, styles[size], className)}
      style={{
        background: `hsl(${hue} 60% 32%)`,
        color: `hsl(${hue} 70% 90%)`,
      }}
      aria-hidden="true"
      title={name}
    >
      {initials(name)}
    </span>
  );
}

function hashHue(str: string): number {
  let h = 0;
  for (let i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) % 360;
  return h;
}
