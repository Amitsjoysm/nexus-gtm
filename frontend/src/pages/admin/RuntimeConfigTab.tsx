import { useCallback, useEffect, useState } from "react";
import { Badge, Button, Card, CardHeader, Field, Input, Select, Skeleton, useToast } from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { RuntimeSetting, WebhookInfo } from "@/lib/types";
import styles from "./RuntimeConfigTab.module.css";

/**
 * Deployment settings that can be changed without a deploy, and the Stripe webhook an operator has
 * to wire up by hand.
 *
 * Only what the server's catalog allows appears here. That is the design: several settings are
 * guards, and a guard that can be switched off from the interface it protects is not a guard. The
 * server refuses the excluded ones with a message saying they are withheld deliberately, so an
 * operator hunting for one is told the reason rather than left thinking they mistyped.
 *
 * Every control carries the effect of changing it, and anything rated medium or high carries a
 * warning about what it costs or breaks. A toggle whose result nobody can state in a sentence is a
 * trap, not a feature — a test on the server asserts every entry has both.
 */

const RISK_TONE: Record<string, "danger" | "warning" | "neutral"> = {
  high: "danger",
  medium: "warning",
  low: "neutral",
};

const RISK_LABEL: Record<string, string> = {
  high: "High impact",
  medium: "Check before changing",
  low: "Safe",
};

function SettingRow({ row, onChanged }: { row: RuntimeSetting; onChanged: () => void }) {
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
  const boolValue = row.value === true;
  const changed = isBool ? false : draft !== String(row.value ?? "");
  const canSave = isBool || (changed && (!needsReason || note.trim().length > 0));

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
      onChanged();
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
      onChanged();
    } catch (err) {
      toast.error("Couldn't reset", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className={styles.setting}>
      <div className={styles.head}>
        <span className={styles.label}>{row.label}</span>
        <Badge tone={RISK_TONE[row.risk] ?? "neutral"} dot={row.risk === "high"}>
          {RISK_LABEL[row.risk] ?? row.risk}
        </Badge>
        {row.overridden && row.in_effect && <Badge tone="success" dot>Live</Badge>}
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
        <p className={row.risk === "high" ? styles.warnHigh : styles.warn}>{row.warning}</p>
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
          <>
            {row.options.length > 0 ? (
              <Select value={draft} onChange={(e) => setDraft(e.target.value)}
                      options={row.options.map((o) => ({ value: o, label: o }))} />
            ) : (
              <Input
                type="number"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                min={row.minimum ?? undefined}
                max={row.maximum ?? undefined}
                step={row.kind === "float" ? "0.05" : "1"}
              />
            )}
            <Button size="sm" loading={busy} disabled={busy || !canSave}
                    onClick={() => void apply(draft)}>
              Save
            </Button>
          </>
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

function WebhookPanel() {
  const api = useApiClient();
  const toast = useToast();
  const [info, setInfo] = useState<WebhookInfo | null>(null);
  const [baseUrl, setBaseUrl] = useState(window.location.origin);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setInfo(await api.webhookInfo());
  }, [api]);

  useEffect(() => {
    load().catch(() => setInfo(null));
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

  if (info === null) return <Skeleton width="100%" height={200} />;

  const fullUrl = `${baseUrl.replace(/\/+$/, "")}${info.path}`;

  return (
    <Card padding="lg">
      <CardHeader
        title="Stripe webhook"
        subtitle="Where Stripe sends subscription and invoice events. The URL goes in the Stripe dashboard — this page tells you exactly what to paste."
      />
      <div className={styles.webhook}>
        <Field label="Your public base URL" hint="Whatever Stripe can reach this deployment on. Not localhost.">
          <Input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)}
                 placeholder="https://app.yourdomain.com" />
        </Field>

        <div className={styles.urlBox}>
          <span className={styles.urlLabel}>Paste this into Stripe</span>
          <code className={styles.url}>{fullUrl}</code>
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
            and the route is live — not that Stripe can reach this host.
          </span>
        </div>
      </div>
    </Card>
  );
}

export function RuntimeConfigTab() {
  const api = useApiClient();
  const [rows, setRows] = useState<RuntimeSetting[] | null>(null);

  const load = useCallback(async () => {
    setRows(await api.runtimeSettings());
  }, [api]);

  useEffect(() => {
    load().catch(() => setRows([]));
  }, [load]);

  if (rows === null) return <Skeleton width="100%" height={360} />;

  const groups = rows.reduce<Record<string, RuntimeSetting[]>>((acc, r) => {
    (acc[r.group] ??= []).push(r);
    return acc;
  }, {});

  return (
    <div className={styles.stack}>
      <Card padding="lg">
        <CardHeader
          title="Runtime configuration"
          subtitle="Changes apply to the running application without a deploy — immediately on the API, within 30 seconds on the worker."
        />
        <p className={styles.intro}>
          Only settings that are safe to change from here are listed. Guards — the SSRF check on
          external databases, security headers, login rate limiting, the block on demo signals in
          production — are deliberately absent and the server refuses them: a guard that can be
          switched off from the interface it protects is not a guard. Everything else on this
          deployment stays changeable by deploy alone.
        </p>
      </Card>

      <WebhookPanel />

      {Object.entries(groups).map(([group, items]) => (
        <Card padding="lg" key={group}>
          <CardHeader title={group} subtitle={`${items.length} setting${items.length === 1 ? "" : "s"}`} />
          <ul className={styles.settings}>
            {items.map((row) => (
              <SettingRow key={row.key} row={row} onChanged={() => void load()} />
            ))}
          </ul>
        </Card>
      ))}
    </div>
  );
}
