import { useAuth } from "@/app/AuthContext";
import { Button } from "@/components/ui";
import styles from "./ImpersonationBanner.module.css";

/**
 * Standing notice that this session belongs to somebody else.
 *
 * Impersonation is read-only and every mutation 403s server-side (`require_writable`), so this
 * banner is not the control — it is the *disclosure*. The risk it addresses is a staff member
 * forgetting which account they are in and reading a customer's data believing it is a demo, or
 * filing a bug against their own workspace.
 *
 * Deliberately not dismissible, and deliberately at the top of the shell rather than inside a
 * page: an impersonation session that can be hidden is one that gets forgotten. The exit is right
 * here, because the alternative to a one-click exit is staying impersonated out of inconvenience.
 */
export function ImpersonationBanner() {
  const { session, endImpersonation } = useAuth();
  if (!session?.readOnly) return null;

  return (
    <div className={styles.banner} role="status">
      <span className={styles.dot} aria-hidden="true" />
      <p className={styles.text}>
        Viewing as <strong>{session.impersonating ?? "another user"}</strong>. Read-only: any change
        you attempt will be refused.
      </p>
      <Button size="sm" variant="secondary" onClick={endImpersonation}>
        End session
      </Button>
    </div>
  );
}
