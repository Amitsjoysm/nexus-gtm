import { useCallback, useEffect, useState } from "react";
import { Badge, Button, Card, CardHeader, Field, Input, Select, Skeleton, useToast } from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { ProviderKey, SupportedProvider } from "@/lib/types";
import styles from "./ProviderKeysTab.module.css";

/**
 * Manage every pooled provider credential without editing deploy/.env or redeploying.
 *
 * Two states this screen exists to make impossible, both measured on 2026-08-21:
 *
 * * All five Groq keys returned 404 because the configured model had been withdrawn, so every
 *   draft came from the stub and reached real prospects with nothing reporting a problem.
 * * Both Apify accounts 403'd on an approval that must be clicked in Apify's console, and the key
 *   that worked a fortnight earlier now 401s.
 *
 * Hence two test depths, and hence `probe_ok` never rendering as a tick: it means the credential
 * authenticates, not that the product works with it.
 */

/** `probe_ok` is deliberately `info`, never `success`. See the note above. */
const STATUS_TONE: Record<string, "success" | "info" | "danger" | "neutral"> = {
  verified: "success",
  probe_ok: "info",
  failed: "danger",
  untested: "neutral",
};

const STATUS_LABEL: Record<string, string> = {
  verified: "Verified",
  probe_ok: "Auth OK",
  failed: "Failed",
  untested: "Untested",
};

export function ProviderKeysTab() {
  const api = useApiClient();
  const toast = useToast();
  const [keys, setKeys] = useState<ProviderKey[] | null>(null);
  const [providers, setProviders] = useState<SupportedProvider[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState({ provider: "", label: "", key: "" });

  const load = useCallback(async () => {
    const [rows, list] = await Promise.all([api.providerKeys(), api.supportedProviders()]);
    setKeys(rows);
    setProviders(list);
    setForm((f) => ({ ...f, provider: f.provider || list[0]?.id || "" }));
  }, [api]);

  useEffect(() => {
    load().catch(() => setKeys([]));
  }, [load]);

  function fail(title: string, err: unknown) {
    toast.error(title, err instanceof ApiError ? err.detail : "Please try again.");
  }

  async function act(id: string, title: string, fn: () => Promise<unknown>) {
    setBusy(id);
    try {
      await fn();
      await load();
    } catch (err) {
      fail(title, err);
    } finally {
      setBusy(null);
    }
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setAdding(true);
    try {
      await api.addProviderKey({
        provider: form.provider, label: form.label.trim(), key: form.key.trim(),
      });
      setForm({ provider: form.provider, label: "", key: "" });
      await load();
      toast.success("Key added", "Probe it to confirm it authenticates.");
    } catch (err) {
      fail("Couldn't add the key", err);
    } finally {
      setAdding(false);
    }
  }

  async function test(row: ProviderKey, depth: "probe" | "verify") {
    setBusy(row.id);
    try {
      const result = await api.testProviderKey(row.id, depth);
      await load();
      if (result.ok) {
        toast.success(
          depth === "verify" ? "A real call succeeded" : "Key authenticates",
          depth === "probe" ? "Verify it to confirm real calls work too." : undefined,
        );
      } else {
        // The provider's own words. "Invalid API Key" and "the model does not exist" need
        // opposite fixes, and the status code alone does not tell them apart.
        toast.error(`${row.provider} key failed`, result.detail);
      }
    } catch (err) {
      fail("Couldn't test the key", err);
    } finally {
      setBusy(null);
    }
  }

  async function remove(row: ProviderKey) {
    if (!window.confirm(`Delete the ${row.provider} key ending ${row.key_hint}? This cannot be undone.`)) {
      return;
    }
    await act(row.id, "Couldn't delete the key", () => api.deleteProviderKey(row.id));
  }

  const grouped = (keys ?? []).reduce<Record<string, ProviderKey[]>>((acc, k) => {
    (acc[k.provider] ??= []).push(k);
    return acc;
  }, {});

  return (
    <div className={styles.stack}>
      <Card padding="lg">
        <CardHeader
          title="Add a key"
          subtitle="Stored encrypted. Live on every process, including the worker, within 30 seconds — no restart."
        />
        <form onSubmit={submit} className={styles.form}>
          <Field label="Provider">
            <Select
              value={form.provider}
              onChange={(e) => setForm({ ...form, provider: e.target.value })}
              options={providers.map((p) => ({ value: p.id, label: p.label }))}
            />
          </Field>
          <Field label="Label" hint="How you'll recognise it later.">
            <Input
              value={form.label}
              onChange={(e) => setForm({ ...form, label: e.target.value })}
              placeholder="primary"
            />
          </Field>
          <Field label="Key" hint="Never shown again after saving.">
            <Input
              type="password"
              value={form.key}
              onChange={(e) => setForm({ ...form, key: e.target.value })}
              required
              minLength={8}
            />
          </Field>
          <Button type="submit" loading={adding} disabled={!form.provider}>
            Add key
          </Button>
        </form>
      </Card>

      {keys === null && <Skeleton width="100%" height={200} />}

      {keys !== null && keys.length === 0 && (
        <Card padding="lg">
          <p className={styles.empty}>
            No managed keys yet. Every provider is running on its environment variable, exactly as
            before — adding one here takes over for that provider only.
          </p>
        </Card>
      )}

      {Object.entries(grouped).map(([provider, rows]) => (
        <Card padding="lg" key={provider}>
          <CardHeader
            title={providers.find((p) => p.id === provider)?.label ?? provider}
            subtitle={`${rows.length} key${rows.length === 1 ? "" : "s"} · the pinned key is tried first`}
          />
          <ul className={styles.keys}>
            {rows.map((row) => (
              <li key={row.id} className={row.enabled ? styles.key : styles.keyOff}>
                <div className={styles.keyHead}>
                  <span className={styles.label}>{row.label || "(no label)"}</span>
                  <code className={styles.hint}>••••{row.key_hint}</code>
                  <Badge tone={STATUS_TONE[row.status] ?? "neutral"} dot>
                    {STATUS_LABEL[row.status] ?? row.status}
                  </Badge>
                  {row.preferred && <Badge tone="info">Pinned</Badge>}
                  {!row.enabled && <Badge tone="neutral">Disabled</Badge>}
                </div>

                {row.status === "probe_ok" && (
                  <p className={styles.note}>
                    Authenticates, but real calls are untested. Verify to be sure — a key can pass
                    auth while every actual request fails.
                  </p>
                )}
                {row.last_error && row.status === "failed" && (
                  <p className={styles.error}>
                    {row.last_error_status ? `${row.last_error_status}: ` : ""}
                    {row.last_error}
                  </p>
                )}

                <div className={styles.actions}>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => test(row, "probe")}
                    loading={busy === row.id}
                    disabled={busy !== null}
                  >
                    Test
                  </Button>
                  {/* Says what it costs, because it is the only action here that spends money. */}
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => test(row, "verify")}
                    disabled={busy !== null}
                    title="Makes a real, billable call through the provider"
                  >
                    Verify (uses credits)
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => act(row.id, "Couldn't pin the key", () => api.preferProviderKey(row.id))}
                    disabled={busy !== null || row.preferred}
                  >
                    {row.preferred ? "Pinned" : "Pin"}
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() =>
                      act(row.id, "Couldn't change the key", () =>
                        api.setProviderKeyEnabled(row.id, !row.enabled))
                    }
                    disabled={busy !== null}
                  >
                    {row.enabled ? "Disable" : "Enable"}
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => remove(row)} disabled={busy !== null}>
                    Delete
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        </Card>
      ))}
    </div>
  );
}
