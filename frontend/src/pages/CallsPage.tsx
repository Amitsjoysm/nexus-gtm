import { useMemo, useState } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Card,
  DataTable,
  EmptyState,
  ErrorState,
  Icons,
  Input,
  Skeleton,
  Textarea,
  useToast,
} from "@/components/ui";
import type { Column } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { CALL_DISPOSITIONS } from "@/lib/types";
import type { CallScript, CallTask } from "@/lib/types";
import styles from "./CallsPage.module.css";

const DISPO_LABEL: Record<string, string> = {
  connected: "Connected",
  voicemail: "Voicemail",
  no_answer: "No answer",
  callback: "Callback",
  meeting_booked: "Meeting booked",
  not_interested: "Not interested",
  bad_number: "Bad number",
  gatekeeper: "Gatekeeper",
};

function telHref(phone: string | null): string | null {
  if (!phone) return null;
  const digits = phone.replace(/[^\d+]/g, "");
  return digits ? `tel:${digits}` : null;
}

export function CallsPage() {
  const api = useApiClient();
  const toast = useToast();
  const queue = useApi<CallTask[]>((signal) => api.callQueue("open", false, signal), []);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [script, setScript] = useState<CallScript | null>(null);
  const [scriptBusy, setScriptBusy] = useState(false);
  const [notes, setNotes] = useState("");
  const [nextStep, setNextStep] = useState("");
  const [logging, setLogging] = useState<string | null>(null);

  const rows = queue.data ?? [];
  const selected = useMemo(() => rows.find((t) => t.id === selectedId) ?? null, [rows, selectedId]);

  function select(task: CallTask) {
    setSelectedId(task.id);
    setScript(null);
    setNotes("");
    setNextStep("");
  }

  async function generate() {
    if (!selected) return;
    setScriptBusy(true);
    try {
      setScript(await api.generateCallScript(selected.id));
    } catch (err) {
      toast.error("Couldn't generate script", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setScriptBusy(false);
    }
  }

  async function logDisposition(disposition: string) {
    if (!selected) return;
    setLogging(disposition);
    try {
      await api.logCallDisposition(selected.id, {
        disposition,
        notes: notes.trim() || undefined,
        next_step: nextStep.trim() || undefined,
      });
      toast.success("Logged", `${DISPO_LABEL[disposition] ?? disposition} — ${selected.contact_name ?? "contact"}.`);
      setSelectedId(null);
      setScript(null);
      queue.refetch();
    } catch (err) {
      toast.error("Couldn't log call", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setLogging(null);
    }
  }

  const columns: Column<CallTask>[] = useMemo(
    () => [
      {
        key: "contact_name",
        header: "Contact",
        sortValue: (t) => t.contact_name,
        render: (t) => <span className={styles.name}>{t.contact_name ?? "—"}</span>,
      },
      {
        key: "account_name",
        header: "Account",
        sortValue: (t) => t.account_name,
        render: (t) => <span className={styles.muted}>{t.account_name}</span>,
      },
      {
        key: "phone",
        header: "Phone",
        hideOnMobile: true,
        sortValue: (t) => t.phone,
        render: (t) => (t.phone ? <span className={styles.mono}>{t.phone}</span> : <span className={styles.muted}>no number</span>),
      },
      {
        key: "priority",
        header: "Priority",
        align: "right",
        sortValue: (t) => t.priority,
        render: (t) => <Badge tone={t.priority >= 70 ? "success" : t.priority >= 40 ? "warning" : "neutral"}>{t.priority}</Badge>,
      },
    ],
    [],
  );

  return (
    <div>
      <PageHeader
        title="Calls"
        description="Your prioritized call list. Open a call for an AI script, dial, and log the outcome."
        actions={
          <span className={styles.count}>{rows.length} to call</span>
        }
      />

      <div className={styles.layout}>
        <Card padding="none" className={styles.queue}>
          {queue.error && !queue.data ? (
            <ErrorState title="Couldn't load the call queue" message={queue.error.detail} onRetry={queue.refetch} />
          ) : (
            <DataTable<CallTask>
              columns={columns}
              rows={rows}
              getRowKey={(t) => t.id}
              loading={queue.loading && !queue.data}
              skeletonRows={6}
              onRowClick={select}
              caption="Call queue"
              empty={
                <EmptyState
                  icon={<Icons.PhoneIcon />}
                  title="No calls queued"
                  description="Calls appear here from cadences with a call step, or add one from an account/contact."
                />
              }
            />
          )}
        </Card>

        <Card className={styles.panel}>
          {!selected ? (
            <div className={styles.placeholder}>
              <Icons.PhoneIcon />
              <p>Select a call to see the AI script and log the outcome.</p>
            </div>
          ) : (
            <div className={styles.call}>
              <header className={styles.callHead}>
                <div>
                  <h2 className={styles.callName}>{selected.contact_name ?? "Unknown contact"}</h2>
                  <p className={styles.callMeta}>
                    {selected.title ? `${selected.title} · ` : ""}{selected.account_name}
                  </p>
                </div>
                {telHref(selected.phone) ? (
                  <a className={styles.dialBtn} href={telHref(selected.phone)!}>
                    <Icons.PhoneIcon /> Call {selected.phone}
                  </a>
                ) : (
                  <Badge tone="warning" dot>No number</Badge>
                )}
              </header>

              {selected.reason && <p className={styles.reason}>{selected.reason}</p>}

              <div className={styles.scriptBlock}>
                <div className={styles.scriptHead}>
                  <h3>AI call script</h3>
                  <Button size="sm" variant="secondary" loading={scriptBusy} onClick={generate}>
                    {script ? "Regenerate" : "Generate script"}
                  </Button>
                </div>
                {scriptBusy && !script && <Skeleton width="100%" height={120} />}
                {script && (
                  <div className={styles.script}>
                    <p><strong>Opener.</strong> {script.opener}</p>
                    <p><strong>Hook.</strong> {script.hook}</p>
                    <p><strong>Value.</strong> {script.value_prop}</p>
                    {script.discovery_questions.length > 0 && (
                      <div>
                        <strong>Discovery</strong>
                        <ul>{script.discovery_questions.map((q, i) => <li key={i}>{q}</li>)}</ul>
                      </div>
                    )}
                    {script.objections.length > 0 && (
                      <div>
                        <strong>Objections</strong>
                        <ul>
                          {script.objections.map((o, i) => (
                            <li key={i}><em>{o.objection}</em> → {o.response}</li>
                          ))}
                        </ul>
                      </div>
                    )}
                    <p><strong>Ask.</strong> {script.cta}</p>
                    <p className={styles.vm}><strong>Voicemail.</strong> {script.voicemail}</p>
                  </div>
                )}
              </div>

              <div className={styles.logBlock}>
                <h3>Log the outcome</h3>
                <Textarea
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                  placeholder="Call notes…"
                  rows={2}
                  aria-label="Call notes"
                />
                <Input
                  value={nextStep}
                  onChange={(e) => setNextStep(e.target.value)}
                  placeholder="Next step (e.g. demo Tue 2pm)"
                  aria-label="Next step"
                />
                <div className={styles.dispoGrid}>
                  {CALL_DISPOSITIONS.map((d) => (
                    <Button
                      key={d}
                      size="sm"
                      variant={d === "meeting_booked" ? "primary" : "secondary"}
                      loading={logging === d}
                      disabled={!!logging}
                      onClick={() => logDisposition(d)}
                    >
                      {DISPO_LABEL[d] ?? d}
                    </Button>
                  ))}
                </div>
              </div>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}

export default CallsPage;
