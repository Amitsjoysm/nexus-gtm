import type { ReactNode } from "react";
import { Link, type LinkProps } from "react-router-dom";
import { cn } from "@/lib/cn";
import type { ButtonSize, ButtonVariant } from "./Button";
import styles from "./Button.module.css";

export interface ButtonLinkProps extends LinkProps {
  variant?: ButtonVariant;
  size?: ButtonSize;
  iconLeft?: ReactNode;
}

/**
 * A link that goes somewhere, drawn exactly like a `Button` beside it.
 *
 * Pages used to restyle a `<Link>` by hand to look like a button, so the two never quite matched:
 * on Replies, "Reply settings" sat 8px taller and a size larger than "Show team" next to it. It is
 * still an anchor, so it opens in a new tab and reads as navigation to a screen reader.
 */
export function ButtonLink({
  variant = "secondary", size = "md", iconLeft, className, children, ...rest
}: ButtonLinkProps) {
  return (
    <Link className={cn(styles.button, styles[variant], styles[size], styles.link, className)} {...rest}>
      {iconLeft && <span className={styles.icon} aria-hidden="true">{iconLeft}</span>}
      <span className={styles.label}>{children}</span>
    </Link>
  );
}
