import { createContext, useContext, type ReactNode } from "react";
import { cn } from "@/lib/cn";
import styles from "./PageHeader.module.css";

export interface PageHeaderProps {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  /** Optional back link / breadcrumb slot rendered above the title. */
  eyebrow?: ReactNode;
  className?: string;
}

/**
 * True inside a page that already has its own title, such as a Settings tab: the page's header is
 * then a section heading (h3) under "Settings" rather than a second page title beside it, and a
 * back link would point out of the frame the user chose to be in.
 */
export const NestedPageContext = createContext(false);

export function useNestedPage(): boolean {
  return useContext(NestedPageContext);
}

/** In-content section header (h2, or h3 when nested) with description and right-aligned actions. */
export function PageHeader({ title, description, actions, eyebrow, className }: PageHeaderProps) {
  const nested = useNestedPage();
  const Heading = nested ? "h3" : "h2";
  return (
    <div className={cn(styles.header, nested && styles.nested, className)}>
      <div className={styles.text}>
        {eyebrow && !nested && <div className={styles.eyebrow}>{eyebrow}</div>}
        <Heading className={cn(styles.title, nested && styles.titleNested)}>{title}</Heading>
        {description &&
          (typeof description === "string" ? (
            <p className={styles.desc}>{description}</p>
          ) : (
            // A rich description carries badges and controls, and a <p> may only hold phrasing
            // content: a <select> wrapper inside one is invalid markup.
            <div className={styles.desc}>{description}</div>
          ))}
      </div>
      {actions && <div className={styles.actions}>{actions}</div>}
    </div>
  );
}
