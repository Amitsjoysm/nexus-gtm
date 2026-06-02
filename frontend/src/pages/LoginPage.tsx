import { useState } from "react";
import type { FormEvent } from "react";
import { Button, Field, Input } from "@/components/ui";
import { LogoMark, TargetIcon, SignalIcon, SparklesIcon } from "@/components/ui/icons";
import { useAuth } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import styles from "./LoginPage.module.css";

type Mode = "login" | "signup";

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
  const { login, signup } = useAuth();
  const [mode, setMode] = useState<Mode>("login");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // Controlled fields (shared across modes where applicable).
  const [companyName, setCompanyName] = useState("");
  const [companySlug, setCompanySlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [tenantSlug, setTenantSlug] = useState("");

  function switchMode(next: Mode) {
    setMode(next);
    setFormError(null);
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setFormError(null);
    setSubmitting(true);
    try {
      if (mode === "login") {
        await login({ email, password, tenant_slug: tenantSlug || null });
      } else {
        await signup({
          company_name: companyName,
          company_slug: companySlug || slugify(companyName),
          full_name: fullName,
          email,
          password,
        });
      }
      // On success the auth guard redirects automatically.
    } catch (err) {
      setFormError(
        err instanceof ApiError
          ? err.detail
          : "Something went wrong. Please try again.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className={styles.page}>
      <section className={styles.aside} aria-hidden="true">
        <div className={styles.brand}>
          <span className={styles.logo}>
            <LogoMark />
          </span>
          <span className={styles.brandText}>NEXUS GTM</span>
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
            <span className={styles.logoSm}>
              <LogoMark />
            </span>
            <span>NEXUS GTM</span>
          </div>

          <h1 className={styles.title}>
            {mode === "login" ? "Welcome back" : "Create your workspace"}
          </h1>
          <p className={styles.subtitle}>
            {mode === "login"
              ? "Sign in to your GTM intelligence workspace."
              : "Spin up a new workspace in seconds — no credit card."}
          </p>

          <form className={styles.form} onSubmit={onSubmit} noValidate>
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

            {mode === "login" && (
              <Field label="Workspace slug" hint="Only needed if you belong to multiple workspaces.">
                <Input
                  value={tenantSlug}
                  onChange={(e) => setTenantSlug(e.target.value)}
                  placeholder="acme-corp (optional)"
                />
              </Field>
            )}

            {formError && (
              <p className={styles.error} role="alert">
                {formError}
              </p>
            )}

            <Button type="submit" size="lg" fullWidth loading={submitting}>
              {mode === "login" ? "Sign in" : "Create workspace"}
            </Button>
          </form>

          <p className={styles.switch}>
            {mode === "login" ? "New to NEXUS?" : "Already have a workspace?"}{" "}
            <button
              type="button"
              className={styles.switchBtn}
              onClick={() => switchMode(mode === "login" ? "signup" : "login")}
            >
              {mode === "login" ? "Create a workspace" : "Sign in"}
            </button>
          </p>
        </div>
      </section>
    </div>
  );
}
