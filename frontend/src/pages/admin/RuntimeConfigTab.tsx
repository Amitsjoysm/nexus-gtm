import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent } from "react";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Field,
  IconButton,
  Icons,
  Input,
  Select,
  Skeleton,
  useToast,
} from "@/components/ui";
import type { BadgeTone } from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { EmailVerifierCheck, RuntimeSetting, WebhookInfo } from "@/lib/types";
import styles from "./RuntimeConfigTab.module.css";

/**
 * Deployment settings that can be changed without a deploy, grouped the way an operator thinks
 * about them, plus the Stripe webhook an operator has to wire up by hand.
 *
 * Only what the server's catalog allows appears here. Several settings are guards, and a guard that
 * can be switched off from the interface it protects is not a guard; the server refuses the
 * excluded ones with a message saying they are withheld deliberately.
 *
 * The group ORDER comes from the server (`GROUP_ORDER` in `runtime_config/catalog.py`), and so does
 * the order inside a group. Sorting here would put "Accounts claimed per tick" above the switch that
 * decides whether anything is claimed at all.
 *
 * The input follows the setting's kind. It used to be a number box for every non-switch setting
 * without options, so an IP allowlist and a verifier URL could not be typed at all.
 */

/** The group carrying Check connection. Matched by name because that is what the server sends. */
const EMAIL_GROUP = "Email finding & verification";
const BILLING_GROUP = "Billing";

const RISK_TONE: Record<string, BadgeTone> = {
  high: "danger",
  medium: "warning",
  low: "neutral",
};

const RISK_LABEL: Record<string, string> = {
  high: "High impact",
  medium: "Check before changing",
  low: "Safe",
};

const CHECK_TONE: Record<EmailVerifierCheck["status"], BadgeTone> = {
  ok: "success",
  degraded: "warning",
  unconfigured: "neutral",
  error: "danger",
};

const CHECK_LABEL: Record<EmailVerifierCheck["status"], string> = {
  ok: "Reachable",
  degraded: "Degraded",
  unconfigured: "Not configured",
  error: "Not reachable",
};

function slug(group: string): string {
  return `rc-${group.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "")}`;
}

function SettingRow({ row, onChanged }: { row: RuntimeSetting; onChanged: (key: string) => void }) {
  const api = useApiClient();
  const toast = useToast();
  const [draft, setDraft] = useState(String(row.value ?? ""));
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setDraft(String(row.value ?? ""));
    setNote("");
  }, [row.value, row.key]);

  // A high-impact change asks for a reason. Six months later "who turned this on and what did they
  // think it did" is the only question that matters, and the audit log can only answer it if
  // somebody wrote it down.
  const needsReason = row.risk === "high";
  const isBool = row.kind === "bool";
  const isNumber = row.kind === "int" || row.kind === "float";
  const boolValue = row.value === true;
  const changed = isBool ? false : draft !== String(row.value ?? "");
  const canSave = isBool || (changed && (!needsReason || note.trim().length > 0));
  const controlId = `rc-control-${row.key}`;
  const labels = row.option_labels ?? {};

  async function apply(value: unknown) {
    setBusy(true);
    try {
      await api.setRuntimeSetting(row.key, value, note.trim());
      toast.success(
        `${row.label} updated`,
        row.requires_restart
          ? "Stored. This one is read at startup, so it applies on the next restart."
          : "Live on the API now; the worker picks it up within 30 seconds.",
      );
      onChanged(row.key);
    } catch (err) {
      toast.error(
        `Couldn't change ${row.label}`,
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    setBusy(true);
    try {
      await api.clearRuntimeSetting(row.key);
      toast.success(`${row.label} reset`, "The deployment's own value applies again.");
      onChanged(row.key);
    } catch (err) {
      toast.error("Couldn't reset", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className={styles.setting}>
      <div className={styles.head}>
        <label className={styles.label} htmlFor={controlId}>{row.label}</label>
        <Badge tone={RISK_TONE[row.risk] ?? "neutral"} dot={row.risk === "high"}>
          {RISK_LABEL[row.risk] ?? row.risk}
        </Badge>
        {row.overridden && row.in_effect && <Badge tone="success" dot>Changed here</Badge>}
        {/* Stored but not what this process is running on. The distinction matters most for the
            restart-only settings, where "saved" is the whole truth until someone restarts. */}
        {row.overridden && !row.in_effect && (
          <Badge tone="warning" dot>Saved, not yet live</Badge>
        )}
        {row.requires_restart && <Badge tone="neutral">Needs restart</Badge>}
        <code className={styles.key}>{row.key}</code>
      </div>

      <p className={styles.effect}>{row.effect}</p>
      {row.warning && (
        <p className={row.risk === "high" ? styles.warnHigh : styles.warn}>
          <span className={styles.warnLabel}>
            {row.risk === "high" ? "Before you change it:" : "Worth knowing:"}
          </span>{" "}
          {row.warning}
        </p>
      )}
      {row.overridden && row.note && (
        <p className={styles.reason}>Changed because: {row.note}</p>
      )}

      {needsReason && !isBool && changed && (
        <Field label="Why are you changing this?" hint="Recorded in the audit log with the old and new value.">
          <Input value={note} onChange={(e) => setNote(e.target.value)}
                 placeholder="Approved by finance for the Q4 push" />
        </Field>
      )}

      <div className={styles.controls}>
        {isBool ? (
          <>
            {/* A high-impact switch asks before it flips. The browser confirm is deliberate: it is
                the one interruption that cannot be clicked past without reading. */}
            <Button
              id={controlId}
              size="sm"
              variant={boolValue ? "secondary" : "primary"}
              loading={busy}
              disabled={busy}
              onClick={() => {
                if (!boolValue && row.risk === "high" &&
                    !window.confirm(`${row.label}\n\n${row.warning}\n\nTurn it on?`)) return;
                void apply(!boolValue);
              }}
            >
              {boolValue ? "Turn off" : "Turn on"}
            </Button>
            <span className={boolValue ? styles.on : styles.off}>
              Currently {boolValue ? "on" : "off"}
            </span>
          </>
        ) : (
          <form
            className={styles.inline}
            onSubmit={(e) => {
              e.preventDefault();
              if (canSave && !busy) void apply(draft);
            }}
          >
            <div className={styles.control}>
              {row.options.length > 0 ? (
                <Select id={controlId} value={draft} onChange={(e) => setDraft(e.target.value)}
                        // An empty option means "the default", and rendered verbatim it was a blank
                        // line an operator could not read. Each setting's `effect` names the default.
                        options={row.options.map((o) => ({ value: o, label: labels[o] ?? (o || "(default)") }))} />
              ) : isNumber ? (
                <Input
                  id={controlId}
                  type="number"
                  inputMode="decimal"
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  min={row.minimum ?? undefined}
                  max={row.maximum ?? undefined}
                  step={row.kind === "float" ? "any" : "1"}
                />
              ) : (
                <Input
                  id={controlId}
                  type={row.key.endsWith("_url") ? "url" : "text"}
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  placeholder={row.placeholder || undefined}
                  spellCheck={false}
                  autoComplete="off"
                />
              )}
            </div>
            <Button type="submit" size="sm" loading={busy} disabled={busy || !canSave}>
              Save
            </Button>
          </form>
        )}
        {isNumber && (row.minimum !== null || row.maximum !== null) && (
          <span className={styles.range}>
            {row.minimum ?? "any"} to {row.maximum ?? "any"}
          </span>
        )}
        {row.overridden && (
          <Button variant="ghost" size="sm" disabled={busy} onClick={() => void reset()}>
            Reset to deployment value
          </Button>
        )}
      </div>
    </li>
  );
}

/** Rendered inside the Billing card rather than as a card of its own: a card in a card is chrome. */
function WebhookPanel() {
  const api = useApiClient();
  const toast = useToast();
  const [info, setInfo] = useState<WebhookInfo | null>(null);
  const [failed, setFailed] = useState(false);
  const [baseUrl, setBaseUrl] = useState(window.location.origin);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setFailed(false);
    try {
      setInfo(await api.webhookInfo());
    } catch {
      setFailed(true);
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  async function test() {
    setBusy(true);
    try {
      const r = await api.testWebhook(baseUrl.trim());
      if (r.ok) toast.success("Signed event accepted", r.detail);
      else toast.error("The endpoint refused it", r.detail);
    } catch (err) {
      toast.error("Couldn't run the test",
                  err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className={styles.subsection} aria-labelledby="rc-webhook-title">
      <h4 id="rc-webhook-title" className={styles.subTitle}>Stripe webhook</h4>
      <p className={styles.effect}>
        Where Stripe sends subscription and invoice events. The URL goes in the Stripe dashboard;
        this tells you exactly what to paste.
      </p>
      {failed ? (
        <ErrorState title="Couldn't load the webhook details" onRetry={() => void load()} />
      ) : info === null ? (
        <Skeleton width="100%" height={160} />
      ) : (
        <div className={styles.webhook}>
          <Field label="Your public base URL" hint="Whatever Stripe can reach this deployment on. Not localhost.">
            <Input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)}
                   placeholder="https://app.yourdomain.com" />
          </Field>

          <div className={styles.urlBox}>
            <span className={styles.urlLabel}>Paste this into Stripe</span>
            <code className={styles.url}>{`${baseUrl.replace(/\/+$/, "")}${info.path}`}</code>
          </div>

          <div className={styles.head}>
            <Badge tone={info.signing_secret_configured ? "success" : "danger"} dot>
              {info.signing_secret_configured
                ? `Signing secret from ${info.signing_secret_source}`
                : "No signing secret"}
            </Badge>
            {info.stripe_account && <Badge tone="neutral">{info.stripe_account}</Badge>}
            <Badge tone={info.livemode ? "warning" : "info"}>
              {info.livemode ? "Live mode" : "Test mode"}
            </Badge>
          </div>

          <ol className={styles.steps}>
            {info.instructions.map((step) => <li key={step}>{step}</li>)}
          </ol>

          <details className={styles.events}>
            <summary>Select these {info.events_handled.length} events in Stripe</summary>
            <ul>{info.events_handled.map((e) => <li key={e}><code>{e}</code></li>)}</ul>
            <p className={styles.effect}>
              Anything else is a delivery Stripe records as failed, which looks like a fault in your
              dashboard even though we ignored it on purpose.
            </p>
          </details>

          <div className={styles.controls}>
            <Button onClick={() => void test()} loading={busy} disabled={busy}>
              Test connection
            </Button>
            <span className={styles.effect}>
              Posts a correctly signed event at our own endpoint. Proves the signing secret verifies
              and the route is live, not that Stripe can reach this host.
            </span>
          </div>
        </div>
      )}
    </section>
  );
}

/** The result of Check connection, kept on screen until the next check or a verifier change. */
function VerifierResult({ check }: { check: EmailVerifierCheck }) {
  return (
    <div className={styles.checkResult} role="status">
      <Badge tone={CHECK_TONE[check.status]} dot>{CHECK_LABEL[check.status]}</Badge>
      <p className={styles.checkDetail}>{check.detail}</p>
      {check.url && <code className={styles.checkUrl}>{check.url}</code>}
    </div>
  );
}

interface Group {
  name: string;
  id: string;
  rows: RuntimeSetting[];
  visible: RuntimeSetting[];
  changed: number;
}

export function RuntimeConfigTab() {
  const api = useApiClient();
  const toast = useToast();
  const [rows, setRows] = useState<RuntimeSetting[] | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [query, setQuery] = useState("");
  const [changedOnly, setChangedOnly] = useState(false);
  const [check, setCheck] = useState<EmailVerifierCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const [active, setActive] = useState<string>("");
  const searchRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    setLoadFailed(false);
    try {
      setRows(await api.runtimeSettings());
    } catch {
      setLoadFailed(true);
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  const onChanged = useCallback((key: string) => {
    // A result about the verifier that was in force a minute ago would read as a result about the
    // one just saved.
    if (key.startsWith("email_verify")) setCheck(null);
    void load();
  }, [load]);

  async function runCheck() {
    setChecking(true);
    try {
      setCheck(await api.checkEmailVerifier());
    } catch (err) {
      toast.error("Couldn't run the check", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setChecking(false);
    }
  }

  const term = query.trim().toLowerCase();
  const filtering = term.length > 0 || changedOnly;

  const groups = useMemo<Group[]>(() => {
    const out: Group[] = [];
    const byName = new Map<string, Group>();
    for (const row of rows ?? []) {
      let group = byName.get(row.group);
      if (!group) {
        group = { name: row.group, id: slug(row.group), rows: [], visible: [], changed: 0 };
        byName.set(row.group, group);
        out.push(group);
      }
      group.rows.push(row);
      if (row.overridden) group.changed += 1;
      const matches =
        (!changedOnly || row.overridden) &&
        (!term || [row.label, row.key, row.effect, row.group].some((s) => s.toLowerCase().includes(term)));
      if (matches) group.visible.push(row);
    }
    return out;
  }, [rows, term, changedOnly]);

  const shownGroups = groups.filter((g) => g.visible.length > 0);
  const total = rows?.length ?? 0;
  const shown = shownGroups.reduce((n, g) => n + g.visible.length, 0);
  const changedTotal = groups.reduce((n, g) => n + g.changed, 0);

  // Highlight the section being read. Only a hint, so it degrades to nothing where the observer is
  // unavailable.
  useEffect(() => {
    if (typeof IntersectionObserver === "undefined" || shownGroups.length === 0) return;
    const observer = new IntersectionObserver(
      (entries) => {
        const top = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
        if (top) setActive(top.target.id);
      },
      { rootMargin: "0px 0px -65% 0px" },
    );
    for (const g of shownGroups) {
      const el = document.getElementById(g.id);
      if (el) observer.observe(el);
    }
    return () => observer.disconnect();
    // Re-observe only when the set of visible sections changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownGroups.map((g) => g.id).join("|")]);

  function jump(e: MouseEvent<HTMLAnchorElement>, id: string) {
    e.preventDefault();
    const section = document.getElementById(id);
    if (!section) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    section.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
    // Move focus with the view, so a keyboard user lands where they asked to go.
    section.querySelector<HTMLElement>("h3")?.focus({ preventScroll: true });
    setActive(id);
  }

  function clearFilters() {
    setQuery("");
    setChangedOnly(false);
    searchRef.current?.focus();
  }

  if (loadFailed) {
    return (
      <div className={styles.stack}>
        <ErrorState title="Couldn't load runtime settings" onRetry={() => void load()} />
      </div>
    );
  }

  if (rows === null) {
    return (
      <div className={styles.layout}>
        <Skeleton width="100%" height={320} />
        <div className={styles.stack}>
          <Skeleton width="100%" height={44} />
          <Skeleton width="100%" height={420} />
        </div>
      </div>
    );
  }

  return (
    <div className={styles.layout}>
      <nav className={styles.index} aria-label="Settings sections">
        <ul className={styles.indexList}>
          {shownGroups.map((g) => (
            <li key={g.id}>
              <a
                href={`#${g.id}`}
                className={styles.indexLink}
                aria-current={active === g.id ? "location" : undefined}
                onClick={(e) => jump(e, g.id)}
              >
                <span className={styles.indexName}>{g.name}</span>
                <span className={styles.indexCount}>
                  {filtering ? `${g.visible.length}/${g.rows.length}` : g.rows.length}
                  {g.changed > 0 && (
                    <span className={styles.indexChanged}>
                      <span aria-hidden="true"> · </span>
                      {g.changed} changed
                    </span>
                  )}
                </span>
              </a>
            </li>
          ))}
        </ul>
      </nav>

      <div className={styles.stack}>
        <p className={styles.intro}>
          Changes apply without a deploy: at once on the API, within 30 seconds on the worker. Guards
          such as the SSRF checks, security headers and login rate limiting are deliberately not
          listed, and the server refuses them.
        </p>

        <div className={styles.toolbar} role="search">
          <div className={styles.search}>
            <Input
              ref={searchRef}
              type="search"
              aria-label="Search settings"
              placeholder="Search by name, key or what it does"
              value={query}
              iconLeft={<Icons.SearchIcon />}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape" && query) {
                  e.preventDefault();
                  setQuery("");
                }
              }}
            />
            {query && (
              <IconButton
                className={styles.clear}
                size="sm"
                label="Clear search"
                icon={<Icons.XIcon />}
                onClick={() => {
                  setQuery("");
                  searchRef.current?.focus();
                }}
              />
            )}
          </div>
          <Button
            size="sm"
            variant={changedOnly ? "primary" : "secondary"}
            aria-pressed={changedOnly}
            onClick={() => setChangedOnly((v) => !v)}
          >
            Changed only{changedTotal > 0 ? ` (${changedTotal})` : ""}
          </Button>
          <span className={styles.count} aria-live="polite">
            {filtering ? `Showing ${shown} of ${total}` : `${total} settings`}
          </span>
        </div>

        {shownGroups.length === 0 ? (
          <Card padding="lg">
            <EmptyState
              title={changedOnly && !term
                ? "Nothing has been changed here"
                : `No setting matches “${query.trim()}”${changedOnly ? " among changed ones" : ""}`}
              description={changedOnly && !term
                ? "Every setting is running on the deployment's own value."
                : "Search covers each setting's name, its key and what it does."}
              action={<Button size="sm" variant="secondary" onClick={clearFilters}>Clear filters</Button>}
            />
          </Card>
        ) : (
          shownGroups.map((g) => (
            <section key={g.id} id={g.id} className={styles.group} aria-labelledby={`${g.id}-title`}>
              <Card padding="lg">
                <CardHeader
                  title={<span id={`${g.id}-title`} tabIndex={-1}>{g.name}</span>}
                  subtitle={[
                    filtering
                      ? `${g.visible.length} of ${g.rows.length} settings`
                      : `${g.rows.length} setting${g.rows.length === 1 ? "" : "s"}`,
                    g.changed > 0 ? `${g.changed} changed here` : "",
                  ].filter(Boolean).join(" · ")}
                  actions={g.name === EMAIL_GROUP ? (
                    <Button size="sm" variant="secondary" loading={checking} disabled={checking}
                            onClick={() => void runCheck()}>
                      Check connection
                    </Button>
                  ) : undefined}
                />
                {g.name === EMAIL_GROUP && check && <VerifierResult check={check} />}
                <ul className={styles.settings}>
                  {g.visible.map((row) => (
                    <SettingRow key={row.key} row={row} onChanged={onChanged} />
                  ))}
                </ul>
                {g.name === BILLING_GROUP && !filtering && <WebhookPanel />}
              </Card>
            </section>
          ))
        )}
      </div>
    </div>
  );
}
