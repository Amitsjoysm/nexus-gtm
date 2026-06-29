import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { Button, Field, Input } from "@/components/ui";
import { useAuth } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import styles from "./LoginPage.module.css";

/**
 * Public page reached from the emailed password-reset link (/reset-password?email=…&token=…).
 * Verifies the token server-side by setting a new password, then sends the user to sign in.
 */
export function ResetPasswordPage() {
  const { resetPassword } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const email = params.get("email") ?? "";
  const token = params.get("token") ?? "";

  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const linkValid = !!email && !!token;

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    if (password !== confirm) {
      setError("Passwords don't match.");
      return;
    }
    setSubmitting(true);
    try {
      await resetPassword(email, token, password);
      setDone(true);
      setTimeout(() => navigate("/login"), 1500);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Couldn't reset your password.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className={styles.page}>
      <section className={styles.formCol}>
        <div className={styles.formCard}>
          <div className={styles.mobileBrand}>
            <img src="/infojoy-logo.png" alt="" className={styles.logoSmImg} />
            <span>Infojoy GTM</span>
          </div>

          <h1 className={styles.title}>Choose a new password</h1>
          <p className={styles.subtitle}>
            {linkValid
              ? `Set a new password for ${email}.`
              : "This reset link is invalid or incomplete."}
          </p>

          {done ? (
            <p className={styles.notice} role="status">
              Your password has been reset. Redirecting you to sign in…
            </p>
          ) : linkValid ? (
            <form className={styles.form} onSubmit={onSubmit} noValidate>
              <Field label="New password" hint="At least 8 characters." required>
                <Input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  autoComplete="new-password"
                  minLength={8}
                  autoFocus
                  required
                />
              </Field>
              <Field label="Confirm new password" required>
                <Input
                  type="password"
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  placeholder="••••••••"
                  autoComplete="new-password"
                  minLength={8}
                  required
                />
              </Field>

              {error && (
                <p className={styles.error} role="alert">
                  {error}
                </p>
              )}

              <Button type="submit" size="lg" fullWidth loading={submitting}>
                Reset password
              </Button>
            </form>
          ) : (
            <p className={styles.error} role="alert">
              Please use the most recent link from your reset email, or request a new one.
            </p>
          )}

          <p className={styles.switch}>
            <Link className={styles.switchBtn} to="/login">
              Back to sign in
            </Link>
          </p>
        </div>
      </section>
    </div>
  );
}

export default ResetPasswordPage;
