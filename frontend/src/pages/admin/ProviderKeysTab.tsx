import { useCallback, useEffect, useState } from "react";
import { Badge, Button, Card, CardHeader, Field, Input, Select, Skeleton, useToast } from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { ProviderKey, ProviderModels, SupportedProvider } from "@/lib/types";
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

/**
 * Choose the model a provider runs. The other half of the 2026-08-21 outage: every key was fine
 * and the *model* had been withdrawn, and changing it meant editing deploy/.env and redeploying.
 *
 * The list comes from the provider, not from us — their catalogue is theirs to change, and it did.
 * A free-text field sits beside it because a model can appear before their list endpoint reports
 * it, and because refusing an unlisted name would mean this screen could not fix the exact outage
 * it exists for.
 */
function ModelPicker({ provider }: { provider: string }) {
  const api = useApiClient();
  const toast = useToast();
  const [state, setState] = useState<ProviderModels | null>(null);
  const [choice, setChoice] = useState("");
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    const data = await api.providerModels(provider);
    setState(data);
    setChoice(data.current);
  }, [api, provider]);

  useEffect(() => {
    load().catch(() =>
      setState({ provider, current: "", overridden: false, models: [], detail: "" }),
    );
  }, [load, provider]);

  async function save(model: string) {
    setSaving(true);
    try {
      await api.setProviderModel(provider, model);
      await load();
      toast.success(
        model ? "Model changed" : "Override cleared",
        "Live on every process, including the worker, within 30 seconds.",
      );
    } catch (err) {
      toast.error(
        "Couldn't change the model",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setSaving(false);
    }
  }

  if (state === null) return <Skeleton width="100%" height={72} />;

  // The provider's list, plus whatever is in force — a model they have stopped listing is still
  // the one running, and dropping it from the dropdown would silently change it on the next save.
  const options = Array.from(new Set([...state.models, state.current].filter(Boolean)));

  return (
    <div className={styles.model}>
      <div className={styles.modelRow}>
        <Field label="Model" hint="Applies to every request this provider serves.">
          {options.length > 0 ? (
            <Select
              value={choice}
              onChange={(e) => setChoice(e.target.value)}
              options={options.map((m) => ({ value: m, label: m }))}
            />
          ) : (
            <Input
              value={choice}
              onChange={(e) => setChoice(e.target.value)}
              placeholder="model id"
            />
          )}
        </Field>
        <Button
          size="sm"
          onClick={() => save(choice)}
          loading={saving}
          disabled={saving || !choice || choice === state.current}
        >
          Use this model
        </Button>
        {state.overridden && (
          <Button variant="ghost" size="sm" onClick={() => save("")} disabled={saving}>
            Reset to env default
          </Button>
        )}
      </div>
      <p className={styles.note}>
        Running <code className={styles.hint}>{state.current || "(none configured)"}</code>
        {state.overridden ? " — chosen here." : " — from the environment."}
        {/* The provider's own words, which arrive lowercase because they are written as clause
            fragments elsewhere. Sentence-cased here rather than at the source: `detail` is also
            read by machines and by the audit log. */}
        {state.detail && ` ${state.detail[0].toUpperCase()}${state.detail.slice(1)}.`}
      </p>
    </div>
  );
}

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

  // Every provider that has keys, PLUS every provider with a model to choose even if it has none.
  // Grouping by key alone would hide the model picker on a deployment running entirely on
  // environment keys — which is every deployment until someone adds the first managed one, and is
  // exactly when a withdrawn model needs changing.
  const modelProviders = providers.filter((p) => p.has_model).map((p) => p.id);
  const cards = Array.from(new Set([...Object.keys(grouped), ...modelProviders]));

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

      {cards.map((provider) => {
        const rows = grouped[provider] ?? [];
        return (
        <Card padding="lg" key={provider}>
          <CardHeader
            title={providers.find((p) => p.id === provider)?.label ?? provider}
            subtitle={
              rows.length === 0
                ? "No managed keys — running on its environment variable"
                : `${rows.length} key${rows.length === 1 ? "" : "s"} · the pinned key is tried first`
            }
          />
          {modelProviders.includes(provider) && <ModelPicker provider={provider} />}
          <ul className={styles.keys}>
            {rows.map((row) => (
              <li key={row.id} className={row.enabled ? styles.key : styles.keyOff}>
                <div className={styles.keyHead}>
                  <span className={styles.label}>{row.label || "(no label)"}</span>
                  <code className={styles.hint}>••••{row.key_hint}</code>
                  <Badge tone={STATUS_TONE[row.status] ?? "neutral"} dot>
                    {STATUS_LABEL[row.status] ?? row.status}
                  </Badge>
                  {/* Which key is actually being spent. Distinct from "Pinned": pinning is what
                      an operator asked for, this is what the resolver is doing — and when a key
                      is disabled or rotation moves on, the two stop agreeing. */}
                  {row.in_use && <Badge tone="success" dot>In use</Badge>}
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
        );
      })}
    </div>
  );
}
