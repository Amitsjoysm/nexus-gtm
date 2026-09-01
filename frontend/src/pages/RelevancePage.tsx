import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
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
import { JOB_LEVELS } from "@/lib/types";
import type {
  IcpDefinition,
  LearnedWeights,
  RelevanceProfile,
  RelevanceProfileInput,
  Role,
  TitleRecommendation,
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
  regions: string;
  postal_codes: string;
  revenue_min: string;
  revenue_max: string;
  required_tech: string;
  buyer_titles: string;
  job_levels: string[];
  title_keywords: string;
  exclude_title_keywords: string;
  employee_min: string;
  employee_max: string;
  product_context: string;
  value_props: ValuePropDraft[];
  weights: Record<WeightKey, number>;
}

const csv = (xs?: string[] | null) => (xs ?? []).join(", ");
const splitCsv = (s: string) =>
  s
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
const numOrNull = (s: string) => (s.trim() === "" ? null : Number(s));

// The four firmographic dimensions the relevance engine blends into a fit score.
const WEIGHT_ORDER = ["industry", "size", "geo", "tech"] as const;
type WeightKey = (typeof WEIGHT_ORDER)[number];
const WEIGHT_LABELS: Record<string, string> = {
  industry: "Industry",
  size: "Company size",
  geo: "Geography",
  tech: "Tech stack",
};
// Engine DEFAULT_WEIGHTS expressed as whole-percent slider positions (sum 100).
const DEFAULT_WEIGHT_PCT: Record<WeightKey, number> = {
  industry: 35,
  size: 30,
  geo: 15,
  tech: 20,
};
const clampPct = (n: number) => Math.max(0, Math.min(100, Math.round(n)));

/** Slider positions (0–100 each) from a stored weights map of fractions, defaulting per key. */
function weightsToPct(stored?: Record<string, number> | null): Record<WeightKey, number> {
  return Object.fromEntries(
    WEIGHT_ORDER.map((k) => {
      const v = stored?.[k];
      return [k, v != null ? clampPct(v * 100) : DEFAULT_WEIGHT_PCT[k]];
    }),
  ) as Record<WeightKey, number>;
}

function toDraft(p: RelevanceProfile): Draft {
  return {
    industries: csv(p.icp.industries),
    countries: csv(p.icp.countries),
    regions: csv(p.icp.regions),
    postal_codes: csv(p.icp.postal_codes),
    revenue_min: p.icp.revenue_min != null ? String(p.icp.revenue_min) : "",
    revenue_max: p.icp.revenue_max != null ? String(p.icp.revenue_max) : "",
    required_tech: csv(p.icp.required_tech),
    buyer_titles: csv(p.icp.buyer_titles),
    job_levels: p.icp.job_levels ?? [],
    title_keywords: csv(p.icp.title_keywords),
    exclude_title_keywords: csv(p.icp.exclude_title_keywords),
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
    weights: weightsToPct(p.icp.weights),
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

  function setWeight(key: WeightKey, value: number) {
    setDraft((d) => (d ? { ...d, weights: { ...d.weights, [key]: clampPct(value) } } : d));
  }

  function resetWeights() {
    setDraft((d) => (d ? { ...d, weights: { ...DEFAULT_WEIGHT_PCT } } : d));
  }

  // ---- AI: draft the ICP from a website + suggest buyer titles ----
  const [website, setWebsite] = useState("");
  const [analyzing, setAnalyzing] = useState(false);
  const [suggesting, setSuggesting] = useState(false);
  const [titleSuggestions, setTitleSuggestions] = useState<TitleRecommendation[]>([]);

  function applyDraftFromProfile(p: RelevanceProfileInput) {
    setDraft((d) => {
      if (!d) return d;
      const icp = p.icp || {};
      return {
        ...d,
        industries: icp.industries?.length ? csv(icp.industries) : d.industries,
        countries: icp.countries?.length ? csv(icp.countries) : d.countries,
        regions: icp.regions?.length ? csv(icp.regions) : d.regions,
        postal_codes: icp.postal_codes?.length ? csv(icp.postal_codes) : d.postal_codes,
        revenue_min: icp.revenue_min != null ? String(icp.revenue_min) : d.revenue_min,
        revenue_max: icp.revenue_max != null ? String(icp.revenue_max) : d.revenue_max,
        required_tech: icp.required_tech?.length ? csv(icp.required_tech) : d.required_tech,
        buyer_titles: icp.buyer_titles?.length ? csv(icp.buyer_titles) : d.buyer_titles,
        job_levels: icp.job_levels?.length ? icp.job_levels : d.job_levels,
        title_keywords: icp.title_keywords?.length ? csv(icp.title_keywords) : d.title_keywords,
        exclude_title_keywords: icp.exclude_title_keywords?.length
          ? csv(icp.exclude_title_keywords)
          : d.exclude_title_keywords,
        employee_min: icp.employee_min != null ? String(icp.employee_min) : d.employee_min,
        employee_max: icp.employee_max != null ? String(icp.employee_max) : d.employee_max,
        product_context: p.product_context?.trim() ? p.product_context : d.product_context,
        value_props:
          p.value_props && p.value_props.length
            ? p.value_props.map((v) => ({
                name: v.name,
                description: v.description ?? "",
                pains_solved: csv(v.pains_solved),
              }))
            : d.value_props,
      };
    });
  }

  async function analyzeFromWebsite() {
    if (!website.trim()) return;
    setAnalyzing(true);
    try {
      const drafted = await api.analyzeWebsite(website.trim());
      const hasData = Boolean(drafted.icp?.industries?.length || drafted.product_context?.trim());
      applyDraftFromProfile(drafted);
      if (hasData) {
        toast.success("Draft ICP generated", "Review and edit below, then Save changes.");
      } else {
        toast.toast({
          tone: "info",
          title: "Couldn't analyze that site",
          description: "Fill the fields manually, or check that search/LLM keys are configured.",
        });
      }
    } catch (err) {
      toast.error("Analysis failed", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setAnalyzing(false);
    }
  }

  async function suggestTitles() {
    if (!draft) return;
    setSuggesting(true);
    try {
      setTitleSuggestions(
        await api.suggestBuyerTitles({
          industries: splitCsv(draft.industries),
          employee_min: numOrNull(draft.employee_min),
          employee_max: numOrNull(draft.employee_max),
          required_tech: splitCsv(draft.required_tech),
          buyer_titles: splitCsv(draft.buyer_titles),
          // The campaign context. Without these the server ranks on firmographics alone and
          // returns the same generic committee however much the user fills in below.
          value_props: draft.value_props
            .filter((vp) => vp.name.trim())
            .map((vp) => ({
              name: vp.name.trim(),
              description: vp.description.trim(),
              pains_solved: splitCsv(vp.pains_solved),
            })),
          product_context: draft.product_context.trim(),
          limit: 10,
        }),
      );
    } catch (err) {
      toast.error(
        "Couldn't suggest titles",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setSuggesting(false);
    }
  }

  function addTitle(title: string) {
    setDraft((d) => {
      if (!d) return d;
      const current = splitCsv(d.buyer_titles);
      if (current.some((t) => t.toLowerCase() === title.toLowerCase())) return d;
      return { ...d, buyer_titles: csv([...current, title]) };
    });
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!draft || !profile.data) return;
    setSaving(true);
    try {
      // Store weights as fractions to match the engine's DEFAULT_WEIGHTS shape. The engine
      // re-normalizes by their sum, so the slider positions only need to be relative.
      const weights = Object.fromEntries(
        WEIGHT_ORDER.map((k) => [k, draft.weights[k] / 100]),
      ) as Record<string, number>;
      const icp: IcpDefinition = {
        industries: splitCsv(draft.industries),
        countries: splitCsv(draft.countries),
        regions: splitCsv(draft.regions),
        postal_codes: splitCsv(draft.postal_codes),
        revenue_min: numOrNull(draft.revenue_min),
        revenue_max: numOrNull(draft.revenue_max),
        required_tech: splitCsv(draft.required_tech),
        buyer_titles: splitCsv(draft.buyer_titles),
        job_levels: draft.job_levels,
        title_keywords: splitCsv(draft.title_keywords),
        exclude_title_keywords: splitCsv(draft.exclude_title_keywords),
        employee_min: numOrNull(draft.employee_min),
        employee_max: numOrNull(draft.employee_max),
        weights,
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
              {canEdit && (
                <Card padding="lg">
                  <SectionHead
                    title="Draft your ICP with AI"
                    description="Paste your website — AI infers your ideal customer profile (industries, size, buyer titles, value props) from what you sell. Everything lands in the editable fields below; review and Save when it looks right."
                  />
                  <div className={styles.websiteRow}>
                    <Input
                      value={website}
                      onChange={(e) => setWebsite(e.target.value)}
                      placeholder="https://yourcompany.com"
                      disabled={analyzing}
                      aria-label="Your website URL"
                    />
                    <Button
                      type="button"
                      iconLeft={<Icons.BoltIcon />}
                      loading={analyzing}
                      onClick={analyzeFromWebsite}
                      disabled={!website.trim()}
                    >
                      Generate ICP
                    </Button>
                  </div>
                </Card>
              )}
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
                  <Field
                    label="States / provinces"
                    hint="Comma-separated. Narrows within the countries above."
                  >
                    <Input
                      value={draft.regions}
                      onChange={(e) => setField("regions", e.target.value)}
                      placeholder="California, Texas, Ontario"
                      disabled={!canEdit}
                    />
                  </Field>
                  <Field
                    label="ZIP / postal codes"
                    hint="Prefixes work — 941 matches every San Francisco code."
                  >
                    <Input
                      value={draft.postal_codes}
                      onChange={(e) => setField("postal_codes", e.target.value)}
                      placeholder="941, 100, SW1"
                      disabled={!canEdit}
                    />
                  </Field>
                  <div className={styles.grid2}>
                    <Field label="Min revenue" hint="Annual, in whole dollars.">
                      <Input
                        type="number"
                        min={0}
                        value={draft.revenue_min}
                        onChange={(e) => setField("revenue_min", e.target.value)}
                        placeholder="10000000"
                        disabled={!canEdit}
                      />
                    </Field>
                    <Field label="Max revenue">
                      <Input
                        type="number"
                        min={0}
                        value={draft.revenue_max}
                        onChange={(e) => setField("revenue_max", e.target.value)}
                        placeholder="500000000"
                        disabled={!canEdit}
                      />
                    </Field>
                  </div>
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
                  <Field label="Buyer titles" hint="Who to target across the buying committee. Comma-separated.">
                    <Input
                      value={draft.buyer_titles}
                      onChange={(e) => setField("buyer_titles", e.target.value)}
                      placeholder="VP Sales, Head of RevOps, CTO"
                      disabled={!canEdit}
                    />
                  </Field>

                  {/* Level + keyword matching. Exact titles miss the way people actually write
                      them: asking for "Facilities Director" never finds "Director of Facilities"
                      or "Head of Facilities". Levels and keywords catch every phrasing at once,
                      and both are optional — leave them blank and matching behaves exactly as it
                      did before they existed. */}
                  <Field
                    label="Job levels"
                    hint="Optional. Combined with the keywords below — leave empty to match any level."
                  >
                    <div className={styles.levelRow}>
                      {JOB_LEVELS.map((lvl) => {
                        const on = draft.job_levels.includes(lvl.value);
                        return (
                          <button
                            key={lvl.value}
                            type="button"
                            className={on ? styles.levelChipOn : styles.levelChip}
                            aria-pressed={on}
                            disabled={!canEdit}
                            onClick={() =>
                              setField(
                                "job_levels",
                                on
                                  ? draft.job_levels.filter((v) => v !== lvl.value)
                                  : [...draft.job_levels, lvl.value],
                              )
                            }
                          >
                            {lvl.label}
                          </button>
                        );
                      })}
                    </div>
                  </Field>
                  <Field
                    label="Title keywords"
                    hint="Any of these in the title. Word order does not matter."
                  >
                    <Input
                      value={draft.title_keywords}
                      onChange={(e) => setField("title_keywords", e.target.value)}
                      placeholder="facilities, workplace, real estate"
                      disabled={!canEdit}
                    />
                  </Field>
                  <Field
                    label="Exclude title keywords"
                    hint="Disqualifies a match — e.g. assistant, deputy, intern."
                  >
                    <Input
                      value={draft.exclude_title_keywords}
                      onChange={(e) => setField("exclude_title_keywords", e.target.value)}
                      placeholder="assistant, deputy, intern"
                      disabled={!canEdit}
                    />
                  </Field>
                  {canEdit && (
                    <div className={styles.suggestBlock}>
                      <Button
                        type="button"
                        variant="secondary"
                        size="sm"
                        iconLeft={<Icons.BoltIcon />}
                        loading={suggesting}
                        onClick={suggestTitles}
                      >
                        Suggest titles (AI)
                      </Button>
                      {titleSuggestions.length > 0 && (
                        <div className={styles.chips}>
                          {titleSuggestions.map((t) => (
                            <button
                              key={t.title}
                              type="button"
                              className={styles.chip}
                              title={`${t.priority_score}/100 · ${t.department} · ${t.reason}`}
                              onClick={() => addTitle(t.title)}
                            >
                              <span className={styles.chipScore}>{t.priority_score}</span>
                              {t.title}
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </Card>

              <Card padding="lg">
                <SectionHead
                  title="Fit weighting"
                  description="How much each firmographic trait counts toward the fit score. Positions are relative — the engine normalizes them — so drag to emphasize what matters for your motion."
                  action={
                    canEdit ? (
                      <Button
                        type="button"
                        variant="secondary"
                        size="sm"
                        iconLeft={<Icons.RefreshIcon />}
                        onClick={resetWeights}
                      >
                        Reset to defaults
                      </Button>
                    ) : undefined
                  }
                />
                <WeightSliders weights={draft.weights} onChange={setWeight} disabled={!canEdit} />
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

      <div style={{ height: "var(--space-4)" }} />
      <LearnedWeightsCard />
    </div>
  );
}

/**
 * Editable fit-weight sliders. Each slider is a relative 0–100 position; the readout shows the
 * normalized share (raw / sum) because that's what actually drives the engine's score.
 */
function WeightSliders({
  weights,
  onChange,
  disabled,
}: {
  weights: Record<WeightKey, number>;
  onChange: (key: WeightKey, value: number) => void;
  disabled: boolean;
}) {
  const total = WEIGHT_ORDER.reduce((s, k) => s + weights[k], 0) || 1;
  return (
    <div className={styles.weights}>
      {WEIGHT_ORDER.map((key) => {
        const share = Math.round((weights[key] / total) * 100);
        return (
          <div key={key} className={styles.weightRow}>
            <label className={styles.weightLabel} htmlFor={`weight-${key}`}>
              {WEIGHT_LABELS[key]}
            </label>
            <input
              id={`weight-${key}`}
              className={styles.slider}
              type="range"
              min={0}
              max={100}
              step={1}
              value={weights[key]}
              disabled={disabled}
              onChange={(e) => onChange(key, Number(e.target.value))}
              aria-label={`${WEIGHT_LABELS[key]} weight`}
              aria-valuetext={`${share}% of fit`}
            />
            <span className={styles.weightMeta}>
              <span className={styles.weightValue}>{share}%</span>
            </span>
          </div>
        );
      })}
    </div>
  );
}

function LearnedWeightsCard() {
  const api = useApiClient();
  const weights = useApi<LearnedWeights>((signal) => api.getLearnedWeights(signal), []);

  return (
    <Card padding="lg">
      <SectionHead
        title="Auto-learned weighting"
        description="As you log won deals, InfoJoy leans the four firmographic weights toward the traits your wins share. Any positions you set above in Fit weighting take priority over what's learned here."
        action={
          weights.data ? (
            weights.data.learned ? (
              <Badge tone="success" dot>
                Learned from {weights.data.sample_size}{" "}
                {weights.data.sample_size === 1 ? "win" : "wins"}
              </Badge>
            ) : (
              <Badge tone="neutral" dot>
                Static defaults
              </Badge>
            )
          ) : undefined
        }
      />
      <DataState
        state={weights}
        errorTitle="Couldn't load scoring weights"
        skeleton={
          <div className={styles.weights}>
            {WEIGHT_ORDER.map((k) => (
              <div key={k} className={styles.weightRow}>
                <Skeleton width="80%" height={14} />
                <Skeleton width="100%" height={8} />
                <Skeleton width="60%" height={14} />
              </div>
            ))}
          </div>
        }
      >
        {(data) => {
          const learned = data.learned;
          return (
            <>
              <div className={styles.weights}>
                {WEIGHT_ORDER.map((key) => {
                  const value = data.weights[key] ?? 0;
                  const base = data.defaults[key] ?? 0;
                  const delta = value - base;
                  const pct = Math.round(value * 100);
                  const showDelta = learned && Math.abs(delta) >= 0.005;
                  const dir = delta >= 0 ? "up" : "down";
                  return (
                    <div key={key} className={styles.weightRow}>
                      <span className={styles.weightLabel}>{WEIGHT_LABELS[key] ?? key}</span>
                      <div
                        className={styles.weightTrack}
                        role="meter"
                        aria-valuenow={pct}
                        aria-valuemin={0}
                        aria-valuemax={100}
                        aria-label={`${WEIGHT_LABELS[key] ?? key} weight ${pct}%`}
                      >
                        <span
                          className={styles.weightFill}
                          data-static={!learned}
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                      <span className={styles.weightMeta}>
                        <span className={styles.weightValue}>{pct}%</span>
                        {showDelta && (
                          <span className={styles.weightDelta} data-dir={dir}>
                            {delta > 0 ? "+" : "−"}
                            {Math.abs(Math.round(delta * 100))}
                          </span>
                        )}
                      </span>
                    </div>
                  );
                })}
              </div>
              <p className={styles.weightsNote}>
                {learned
                  ? "These weights are tuned from your recorded outcomes and override the defaults. Explicit weights you set in the profile still take priority."
                  : "Scoring is on the neutral defaults. Log a few won deals to start tuning the weights to your motion."}
              </p>
            </>
          );
        }}
      </DataState>
    </Card>
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
