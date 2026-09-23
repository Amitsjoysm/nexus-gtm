import { useState } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Button, Card, EmptyState, ErrorState, Field, Icons, Input, Modal, Skeleton, Textarea,
} from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { StepsEditor, blankStep, stepsProblem } from "@/components/engagement/StepsEditor";
import { WEEKDAYS } from "@/components/engagement/labels";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { EngagementStep, SequenceTemplate } from "@/lib/types";
import styles from "./Engagement.module.css";

/**
 * Sequence templates (spec §9: Cadences becomes Sequence templates): a named set of steps a
 * campaign can start from. A campaign copies the steps when it is created, so editing a template
 * later never changes a campaign that is already running.
 */

interface Draft {
  id: string | null;
  name: string;
  description: string;
  steps: EngagementStep[];
}

function summary(steps: EngagementStep[]): string {
  const emails = steps.filter((s) => s.channel === "email").length;
  const calls = steps.length - emails;
  const days = steps.slice(1).reduce((n, s) => n + (s.delay_business_days || 0), 0);
  const parts = [`${emails} ${emails === 1 ? "email" : "emails"}`];
  if (calls) parts.push(`${calls} ${calls === 1 ? "call" : "calls"}`);
  parts.push(`over ${days} business ${days === 1 ? "day" : "days"}`);
  return parts.join(", ");
}

export function SequenceTemplatesPage() {
  const api = useApiClient();
  const toast = useToast();
  const templates = useApi<SequenceTemplate[]>((s) => api.listSequenceTemplates(s), []);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<SequenceTemplate | null>(null);
  const [busyDelete, setBusyDelete] = useState(false);

  const problem = draft ? (!draft.name.trim() ? "Give the template a name." : stepsProblem(draft.steps)) : null;

  async function save() {
    if (!draft || problem) return;
    setSaving(true);
    try {
      const body = { name: draft.name.trim(), description: draft.description.trim(), steps: draft.steps };
      if (draft.id) await api.updateSequenceTemplate(draft.id, body);
      else await api.createSequenceTemplate(body);
      toast.success(draft.id ? "Template saved" : "Template created",
        "New campaigns can start from it. Running campaigns keep their own steps.");
      setDraft(null);
      templates.refetch();
    } catch (err) {
      toast.error("Template not saved", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!deleting) return;
    setBusyDelete(true);
    try {
      await api.deleteSequenceTemplate(deleting.id);
      toast.success("Template deleted", "Campaigns created from it are unchanged.");
      setDeleting(null);
      templates.refetch();
    } catch (err) {
      toast.error("Couldn't delete it", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusyDelete(false);
    }
  }

  const newTemplate = () => setDraft({ id: null, name: "", description: "", steps: [blankStep(true), blankStep(), blankStep()] });

  return (
    <div className={styles.page}>
      <PageHeader
        title="Sequence templates"
        description="Reusable steps a campaign can start from. A campaign copies them when it is created, so changing a template never changes one that is running."
        actions={!draft && (
          <Button iconLeft={<Icons.PlusIcon />} onClick={newTemplate}>New template</Button>
        )}
      />

      {draft && (
        <Card padding="lg" className={styles.section}>
          <h2 className={styles.sectionTitle}>{draft.id ? "Edit template" : "New template"}</h2>
          <div className={styles.fields}>
            <Field label="Name" required>
              <Input value={draft.name} maxLength={200} autoFocus
                onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="Three touches, two weeks" />
            </Field>
            <Field label="When to use it">
              <Textarea rows={2} value={draft.description}
                onChange={(e) => setDraft({ ...draft, description: e.target.value })}
                placeholder="Cold outreach to engineering leaders after a funding round" />
            </Field>
          </div>
          <StepsEditor steps={draft.steps} onChange={(steps) => setDraft({ ...draft, steps })} />
          {problem && <p className={styles.muted}>{problem}</p>}
          <div className={styles.formActions}>
            <Button variant="ghost" onClick={() => setDraft(null)}>Cancel</Button>
            <Button onClick={save} loading={saving} disabled={Boolean(problem)}>
              {draft.id ? "Save template" : "Create template"}
            </Button>
          </div>
        </Card>
      )}

      {templates.error ? (
        <ErrorState title="Couldn't load templates" message={templates.error.detail} onRetry={templates.refetch} />
      ) : !templates.data ? (
        <div className={styles.stack}>
          <Skeleton width="100%" height={96} />
          <Skeleton width="100%" height={96} />
        </div>
      ) : templates.data.length === 0 ? (
        !draft && (
          <EmptyState
            icon={<Icons.FileTextIcon />}
            title="No templates yet"
            description="Save the steps that work for your team once, and start every campaign from them."
            action={<Button iconLeft={<Icons.PlusIcon />} onClick={newTemplate}>New template</Button>}
          />
        )
      ) : (
        <ul className={styles.templateList}>
          {templates.data.map((t) => (
            <li key={t.id}>
              <Card padding="lg" className={styles.templateCard}>
                <div className={styles.templateHead}>
                  <div>
                    <h3 className={styles.templateName}>{t.name}</h3>
                    <p className={styles.muted}>{summary(t.steps)}</p>
                  </div>
                  <div className={styles.rowActions}>
                    <Button size="sm" variant="secondary" disabled={draft !== null}
                      onClick={() => setDraft({ id: t.id, name: t.name, description: t.description, steps: t.steps.map((s) => ({ ...s })) })}>
                      Edit
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setDeleting(t)}>Delete</Button>
                  </div>
                </div>
                {t.description && <p className={styles.bodyText}>{t.description}</p>}
                <ol className={styles.stepList}>
                  {t.steps.map((s, i) => (
                    <li key={i} className={styles.stepRow}>
                      <span className={styles.stepNumber} aria-hidden="true">{i + 1}</span>
                      <span>
                        {i === 0 ? "Opening email" : s.channel === "call" ? "Call" : "Follow-up email"}
                        {i > 0 && <span className={styles.muted}> after {s.delay_business_days} business {s.delay_business_days === 1 ? "day" : "days"}</span>}
                        {s.angle && <span className={styles.muted}>: {s.angle}</span>}
                        {s.allowed_weekdays.length < 5 || s.allowed_weekdays.some((d) => d > 4) ? (
                          <span className={styles.muted}> ({s.allowed_weekdays.map((d) => WEEKDAYS[d]).join(", ")})</span>
                        ) : null}
                      </span>
                    </li>
                  ))}
                </ol>
              </Card>
            </li>
          ))}
        </ul>
      )}

      <Modal
        open={deleting !== null}
        onClose={() => setDeleting(null)}
        title={`Delete ${deleting?.name ?? "this template"}?`}
        description="Campaigns already created from it keep their steps."
        footer={
          <>
            <Button variant="ghost" onClick={() => setDeleting(null)}>Cancel</Button>
            <Button variant="danger" loading={busyDelete} onClick={remove}>Delete template</Button>
          </>
        }
      >
        <p className={styles.muted}>It will no longer be offered when starting a campaign.</p>
      </Modal>
    </div>
  );
}

export default SequenceTemplatesPage;
