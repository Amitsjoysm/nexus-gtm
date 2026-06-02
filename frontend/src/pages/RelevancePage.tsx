import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Button,
  Card,
  Field,
  Icons,
  IconButton,
  Input,
  Skeleton,
  Textarea,
  useToast,
} from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient, useAuth } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type {
  IcpDefinition,
  RelevanceProfile,
  RelevanceProfileInput,
  Role,
  ValueProp,
} from "@/lib/types";
import styles from "./RelevancePage.module.css";

const ROLE_RANK: Record<Role, number> = { owner: 3, admin: 2, manager: 1, rep: 0 };

interface ValuePropDraft {
  name: string;
  description: string;
  pains_solved: string;
}

interface Draft {
  industries: string;
  countries: string;
  required_tech: string;
  employee_min: string;
  employee_max: string;
  product_context: string;
  value_props: ValuePropDraft[];
}

const csv = (xs?: string[] | null) => (xs ?? []).join(", ");
const splitCsv = (s: string) =>
  s
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
const numOrNull = (s: string) => (s.trim() === "" ? null : Number(s));

function toDraft(p: RelevanceProfile): Draft {
  return {
    industries: csv(p.icp.industries),
    countries: csv(p.icp.countries),
    required_tech: csv(p.icp.required_tech),
    employee_min: p.icp.employee_min != null ? String(p.icp.employee_min) : "",
    employee_max: p.icp.employee_max != null ? String(p.icp.employee_max) : "",
    product_context: p.product_context ?? "",
    value_props:
      p.value_props.length > 0
        ? p.value_props.map((v) => ({
            name: v.name,
            description: v.description ?? "",
            pains_solved: csv(v.pains_solved),
          }))
        : [{ name: "", description: "", pains_solved: "" }],
  };
}

export function RelevancePage() {
  const api = useApiClient();
  const toast = useToast();
  const { session } = useAuth();
  const canEdit = session ? ROLE_RANK[session.role] >= ROLE_RANK.admin : false;

  const profile = useApi<RelevanceProfile>((signal) => api.getRelevanceProfile(signal), []);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);

  // Hydrate the editable draft once the profile loads (and after a save refetch).
  useEffect(() => {
    if (profile.data) setDraft(toDraft(profile.data));
  }, [profile.data]);

  function setField<K extends keyof Draft>(key: K, value: Draft[K]) {
    setDraft((d) => (d ? { ...d, [key]: value } : d));
  }

  function setVP(i: number, key: keyof ValuePropDraft, value: string) {
    setDraft((d) =>
      d
        ? {
            ...d,
            value_props: d.value_props.map((vp, idx) =>
              idx === i ? { ...vp, [key]: value } : vp,
            ),
          }
        : d,
    );
  }

  function addVP() {
    setDraft((d) =>
      d ? { ...d, value_props: [...d.value_props, { name: "", description: "", pains_solved: "" }] } : d,
    );
  }

  function removeVP(i: number) {
    setDraft((d) => (d ? { ...d, value_props: d.value_props.filter((_, idx) => idx !== i) } : d));
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!draft || !profile.data) return;
    setSaving(true);
    try {
      const icp: IcpDefinition = {
        industries: splitCsv(draft.industries),
        countries: splitCsv(draft.countries),
        required_tech: splitCsv(draft.required_tech),
        employee_min: numOrNull(draft.employee_min),
        employee_max: numOrNull(draft.employee_max),
        // Preserve scoring weights — they're tuned elsewhere, not in this editor.
        weights: profile.data.icp.weights,
      };
      const value_props: ValueProp[] = draft.value_props
        .filter((vp) => vp.name.trim())
        .map((vp) => ({
          name: vp.name.trim(),
          description: vp.description.trim() || undefined,
          pains_solved: splitCsv(vp.pains_solved),
        }));
      const body: RelevanceProfileInput = {
        icp,
        value_props,
        product_context: draft.product_context.trim(),
      };
      const updated = await api.updateRelevanceProfile(body);
      profile.setData(updated);
      toast.success("Relevance saved", "New accounts will be scored against this profile.");
    } catch (err) {
      toast.error(
        "Couldn't save",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <div>
      <PageHeader
        title="Relevance"
        description="Define your ideal customer and value props. This drives fit scoring across every account."
        actions={
          canEdit ? (
            <Button
              form="relevance-form"
              type="submit"
              iconLeft={<Icons.CheckIcon />}
              loading={saving}
              disabled={!draft}
            >
              Save changes
            </Button>
          ) : undefined
        }
      />

      {!canEdit && (
        <div className={styles.notice} role="note">
          <Icons.AlertTriangleIcon />
          <span>
            You can review the profile, but only admins and owners can change it.
          </span>
        </div>
      )}

      <DataState
        state={profile}
        errorTitle="Couldn't load relevance profile"
        skeleton={
          <div className={styles.stack}>
            <Card padding="lg">
              <Skeleton width="30%" height={18} />
              <div style={{ height: "var(--space-4)" }} />
              <Skeleton width="100%" height={96} />
            </Card>
            <Card padding="lg">
              <Skeleton width="100%" height={120} />
            </Card>
          </div>
        }
      >
        {() =>
          draft ? (
            <form id="relevance-form" className={styles.stack} onSubmit={onSubmit} noValidate>
              <Card padding="lg">
                <SectionHead
                  title="Ideal customer profile"
                  description="Accounts matching these traits score higher on fit."
                />
                <div className={styles.fields}>
                  <Field label="Industries" hint="Comma-separated.">
                    <Input
                      value={draft.industries}
                      onChange={(e) => setField("industries", e.target.value)}
                      placeholder="Software, Fintech, E-commerce"
                      disabled={!canEdit}
                    />
                  </Field>
                  <Field label="Countries" hint="Comma-separated.">
                    <Input
                      value={draft.countries}
                      onChange={(e) => setField("countries", e.target.value)}
                      placeholder="United States, Canada, United Kingdom"
                      disabled={!canEdit}
                    />
                  </Field>
                  <div className={styles.grid2}>
                    <Field label="Min employees">
                      <Input
                        type="number"
                        min={0}
                        value={draft.employee_min}
                        onChange={(e) => setField("employee_min", e.target.value)}
                        placeholder="50"
                        disabled={!canEdit}
                      />
                    </Field>
                    <Field label="Max employees">
                      <Input
                        type="number"
                        min={0}
                        value={draft.employee_max}
                        onChange={(e) => setField("employee_max", e.target.value)}
                        placeholder="5000"
                        disabled={!canEdit}
                      />
                    </Field>
                  </div>
                  <Field label="Required tech" hint="Accounts must use these. Comma-separated.">
                    <Input
                      value={draft.required_tech}
                      onChange={(e) => setField("required_tech", e.target.value)}
                      placeholder="Snowflake, Segment"
                      disabled={!canEdit}
                    />
                  </Field>
                </div>
              </Card>

              <Card padding="lg">
                <SectionHead
                  title="Value propositions"
                  description="What you sell and the pains it solves. Agents use these to personalize outreach."
                  action={
                    canEdit ? (
                      <Button
                        type="button"
                        variant="secondary"
                        size="sm"
                        iconLeft={<Icons.PlusIcon />}
                        onClick={addVP}
                      >
                        Add value prop
                      </Button>
                    ) : undefined
                  }
                />
                <div className={styles.vpList}>
                  {draft.value_props.map((vp, i) => (
                    <div key={i} className={styles.vp}>
                      <div className={styles.vpHead}>
                        <span className={styles.vpIndex}>{i + 1}</span>
                        {canEdit && draft.value_props.length > 1 && (
                          <IconButton
                            label={`Remove value prop ${i + 1}`}
                            icon={<Icons.TrashIcon />}
                            variant="ghost"
                            onClick={() => removeVP(i)}
                          />
                        )}
                      </div>
                      <Field label="Name" hideLabel>
                        <Input
                          value={vp.name}
                          onChange={(e) => setVP(i, "name", e.target.value)}
                          placeholder="Value prop name"
                          disabled={!canEdit}
                        />
                      </Field>
                      <Field label="Description" hideLabel>
                        <Textarea
                          rows={2}
                          value={vp.description}
                          onChange={(e) => setVP(i, "description", e.target.value)}
                          placeholder="What it does and who it's for."
                          disabled={!canEdit}
                        />
                      </Field>
                      <Field label="Pains solved" hint="Comma-separated.">
                        <Input
                          value={vp.pains_solved}
                          onChange={(e) => setVP(i, "pains_solved", e.target.value)}
                          placeholder="Slow reporting, manual data entry"
                          disabled={!canEdit}
                        />
                      </Field>
                    </div>
                  ))}
                </div>
              </Card>

              <Card padding="lg">
                <SectionHead
                  title="Product context"
                  description="Background the AI agents reference when researching and writing."
                />
                <Field label="Product context" hideLabel>
                  <Textarea
                    rows={5}
                    value={draft.product_context}
                    onChange={(e) => setField("product_context", e.target.value)}
                    placeholder="Describe your product, category, and differentiators."
                    disabled={!canEdit}
                  />
                </Field>
              </Card>

              {canEdit && (
                <div className={styles.footer}>
                  <Button type="submit" iconLeft={<Icons.CheckIcon />} loading={saving}>
                    Save changes
                  </Button>
                </div>
              )}
            </form>
          ) : null
        }
      </DataState>
    </div>
  );
}

function SectionHead({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <div className={styles.sectionHead}>
      <div>
        <h2 className={styles.sectionTitle}>{title}</h2>
        <p className={styles.sectionDesc}>{description}</p>
      </div>
      {action}
    </div>
  );
}
