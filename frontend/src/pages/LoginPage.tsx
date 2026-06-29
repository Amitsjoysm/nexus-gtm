import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Button, Field, Input } from "@/components/ui";
import { TargetIcon, SignalIcon, SparklesIcon } from "@/components/ui/icons";
import { useAuth } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import styles from "./LoginPage.module.css";

type Mode = "login" | "signup" | "forgot";

const HIGHLIGHTS = [
  { icon: <TargetIcon />, text: "Score every account against your ICP automatically." },
  { icon: <SignalIcon />, text: "Real-time buying signals, prioritized into one inbox." },
  { icon: <SparklesIcon />, text: "AI agents research accounts and draft outreach for you." },
];

function slugify(value: string): string {
  return value
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
}

export function LoginPage() {
  const { login, registerStart, registerResend, registerVerify, forgotPassword } = useAuth();
  const [mode, setMode] = useState<Mode>("login");
  // Signup is two-step: collect details ("form"), then verify the emailed code ("verify").
  const [step, setStep] = useState<"form" | "verify">("form");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Controlled fields (shared across modes where applicable).
  const [companyName, setCompanyName] = useState("");
  const [companySlug, setCompanySlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [tenantSlug, setTenantSlug] = useState("");
  const [code, setCode] = useState("");
  const [resendIn, setResendIn] = useState(0); // cooldown countdown (seconds) for "resend code"

  // Tick down the resend cooldown.
  useEffect(() => {
    if (resendIn <= 0) return;
    const t = setTimeout(() => setResendIn((s) => s - 1), 1000);
    return () => clearTimeout(t);
  }, [resendIn]);

  function switchMode(next: Mode) {
    setMode(next);
    setStep("form");
    setCode("");
    setFormError(null);
    setNotice(null);
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setFormError(null);
    setNotice(null);
    setSubmitting(true);
    try {
      if (mode === "login") {
        await login({ email, password, tenant_slug: tenantSlug || null });
      } else if (mode === "forgot") {
        // Generic acknowledgement regardless of whether the email exists (no enumeration).
        await forgotPassword(email);
        setNotice("If an account exists for that email, a password-reset link is on its way.");
      } else if (step === "form") {
        // Step 1: validate + email a verification code. No account exists yet.
        const res = await registerStart({
          company_name: companyName,
          company_slug: companySlug || slugify(companyName),
          full_name: fullName,
          email,
          password,
        });
        setStep("verify");
        setResendIn(res.resend_in_s);
        setNotice(`We emailed a verification code to ${res.email}. It expires in ${Math.round(res.expires_in_s / 60)} minutes.`);
      } else {
        // Step 2: verify the code -> establishes the session; the auth guard redirects.
        await registerVerify(email, code.trim());
      }
    } catch (err) {
      setFormError(
        err instanceof ApiError ? err.detail : "Something went wrong. Please try again.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  async function onResend() {
    setFormError(null);
    setNotice(null);
    try {
      const res = await registerResend(email);
      setResendIn(res.resend_in_s);
      setNotice(`A new code is on its way to ${res.email}.`);
    } catch (err) {
      setFormError(err instanceof ApiError ? err.detail : "Couldn't resend the code.");
    }
  }

  const verifying = mode === "signup" && step === "verify";

  return (
    <div className={styles.page}>
      <section className={styles.aside} aria-hidden="true">
        <div className={styles.brand}>
          <img src="/infojoy-logo.png" alt="Infojoy" className={styles.logoImg} />
          <span className={styles.brandText}>Infojoy GTM</span>
        </div>
        <h2 className={styles.headline}>
          Turn buying signals into pipeline — before your competitors notice.
        </h2>
        <ul className={styles.highlights}>
          {HIGHLIGHTS.map((h, i) => (
            <li key={i} className={styles.highlight}>
              <span className={styles.hIcon}>{h.icon}</span>
              <span>{h.text}</span>
            </li>
          ))}
        </ul>
      </section>

      <section className={styles.formCol}>
        <div className={styles.formCard}>
          <div className={styles.mobileBrand}>
            <img src="/infojoy-logo.png" alt="" className={styles.logoSmImg} />
            <span>Infojoy GTM</span>
          </div>

          <h1 className={styles.title}>
            {verifying
              ? "Verify your email"
              : mode === "forgot"
                ? "Reset your password"
                : mode === "login"
                  ? "Welcome back"
                  : "Create your workspace"}
          </h1>
          <p className={styles.subtitle}>
            {verifying
              ? "Enter the code we emailed you to finish creating your workspace."
              : mode === "forgot"
                ? "Enter your account email and we'll send you a reset link."
                : mode === "login"
                  ? "Sign in to your GTM intelligence workspace."
                  : "Spin up a new workspace in seconds — no credit card."}
          </p>

          <form className={styles.form} onSubmit={onSubmit} noValidate>
            {notice && (
              <p className={styles.notice} role="status">
                {notice}
              </p>
            )}

            {verifying ? (
              <Field label="Verification code" hint="6-digit code from your email.">
                <Input
                  value={code}
                  onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 8))}
                  placeholder="123456"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  autoFocus
                  required
                />
              </Field>
            ) : (
              <>
            {mode === "signup" && (
              <>
                <Field label="Company name" required>
                  <Input
                    value={companyName}
                    onChange={(e) => {
                      setCompanyName(e.target.value);
                      if (!slugTouched) setCompanySlug(slugify(e.target.value));
                    }}
                    placeholder="Acme Corp"
                    autoComplete="organization"
                    required
                  />
                </Field>
                <Field label="Workspace URL slug" hint="Lowercase letters, numbers and dashes." required>
                  <Input
                    value={companySlug}
                    onChange={(e) => {
                      setSlugTouched(true);
                      setCompanySlug(slugify(e.target.value));
                    }}
                    placeholder="acme-corp"
                    required
                  />
                </Field>
                <Field label="Your full name" required>
                  <Input
                    value={fullName}
                    onChange={(e) => setFullName(e.target.value)}
                    placeholder="Jordan Rivera"
                    autoComplete="name"
                    required
                  />
                </Field>
              </>
            )}

            <Field label="Work email" required>
              <Input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@company.com"
                autoComplete="email"
                required
              />
            </Field>

            {mode !== "forgot" && (
              <Field
                label="Password"
                hint={mode === "signup" ? "At least 8 characters." : undefined}
                required
              >
                <Input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  autoComplete={mode === "login" ? "current-password" : "new-password"}
                  minLength={8}
                  required
                />
              </Field>
            )}

            {mode === "login" && (
              <Field label="Workspace slug" hint="Only needed if you belong to multiple workspaces.">
                <Input
                  value={tenantSlug}
                  onChange={(e) => setTenantSlug(e.target.value)}
                  placeholder="acme-corp (optional)"
                />
              </Field>
            )}
              </>
            )}

            {formError && (
              <p className={styles.error} role="alert">
                {formError}
              </p>
            )}

            <Button type="submit" size="lg" fullWidth loading={submitting}>
              {verifying
                ? "Verify & create workspace"
                : mode === "forgot"
                  ? "Send reset link"
                  : mode === "login"
                    ? "Sign in"
                    : "Send verification code"}
            </Button>
          </form>

          {verifying ? (
            <p className={styles.switch}>
              <button
                type="button"
                className={styles.switchBtn}
                onClick={onResend}
                disabled={resendIn > 0}
              >
                {resendIn > 0 ? `Resend code in ${resendIn}s` : "Resend code"}
              </button>
              {" · "}
              <button type="button" className={styles.switchBtn} onClick={() => setStep("form")}>
                Edit details
              </button>
            </p>
          ) : mode === "forgot" ? (
            <p className={styles.switch}>
              <button type="button" className={styles.switchBtn} onClick={() => switchMode("login")}>
                Back to sign in
              </button>
            </p>
          ) : (
            <p className={styles.switch}>
              {mode === "login" && (
                <>
                  <button
                    type="button"
                    className={styles.switchBtn}
                    onClick={() => switchMode("forgot")}
                  >
                    Forgot password?
                  </button>
                  <br />
                </>
              )}
              {mode === "login" ? "New to Infojoy?" : "Already have a workspace?"}{" "}
              <button
                type="button"
                className={styles.switchBtn}
                onClick={() => switchMode(mode === "login" ? "signup" : "login")}
              >
                {mode === "login" ? "Create a workspace" : "Sign in"}
              </button>
            </p>
          )}
        </div>
      </section>
    </div>
  );
}
