import { useMemo, useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Field,
  Icons,
  Input,
  Modal,
  Select,
  Skeleton,
  useToast,
} from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { timeAgo } from "@/lib/format";
import {
  GOAL_DESCRIPTION,
  RUN_STATUS_LABEL,
  RUN_STATUS_TONE,
  goalLabel,
} from "@/lib/runStatus";
import type { Account, Run, RunGoal } from "@/lib/types";
import styles from "./RunsPage.module.css";

const GOAL_OPTIONS: { value: RunGoal; label: string }[] = [
  { value: "research_account", label: goalLabel("research_account") },
  { value: "research_only", label: goalLabel("research_only") },
];

export function RunsPage() {
  const api = useApiClient();
  const toast = useToast();
  const navigate = useNavigate();

  const runs = useApi<Run[]>((signal) => api.listRuns(signal), []);
  const accounts = useApi<Account[]>((signal) => api.listAccounts(signal), []);

  const [open, setOpen] = useState(false);
  const [goal, setGoal] = useState<RunGoal>("research_account");
  const [accountId, setAccountId] = useState("");
  const [sequence, setSequence] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const accountOptions = useMemo(
    () => (accounts.data ?? []).map((a) => ({ value: a.id, label: a.name })),
    [accounts.data],
  );

  function resetForm() {
    setGoal("research_account");
    setAccountId("");
    setSequence("");
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!accountId) return;
    setSubmitting(true);
    try {
      const input: Record<string, unknown> = {};
      if (goal === "research_account" && sequence.trim()) input.sequence = sequence.trim();
      const run = await api.createRun({
        goal,
        account_id: accountId,
        input,
        // Idempotency: a double-submit returns the same run instead of planning twice.
        idempotency_key: crypto.randomUUID(),
      });
      toast.success("Run started", goalLabel(run.goal));
      setOpen(false);
      resetForm();
      navigate(`/runs/${run.id}`);
    } catch (err) {
      toast.error(
        "Couldn't start the run",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  const noAccounts = !accounts.loading && (accounts.data?.length ?? 0) === 0;

  return (
    <div>
      <PageHeader
        title="AI Runs"
        description="Multi-agent workflows that research an account, score its fit, draft outreach, and park the send for your approval."
        actions={
          <Button
            iconLeft={<Icons.PlusIcon />}
            onClick={() => setOpen(true)}
            disabled={noAccounts}
            title={noAccounts ? "Add an account first" : undefined}
          >
            New run
          </Button>
        }
      />

      <DataState
        state={runs}
        errorTitle="Couldn't load runs"
        skeleton={
          <div className={styles.list}>
            {[0, 1, 2].map((i) => (
              <Card key={i} padding="lg">
                <Skeleton width="40%" height={18} />
                <div style={{ height: "var(--space-3)" }} />
                <Skeleton width="100%" height={36} />
              </Card>
            ))}
          </div>
        }
        isEmpty={(rows) => rows.length === 0}
        empty={
          <EmptyState
            icon={<Icons.WorkflowIcon />}
            title="No runs yet"
            description="Kick off your first agent run to turn a target account into reviewed, personalized outreach."
            action={
              <Button
                iconLeft={<Icons.PlusIcon />}
                onClick={() => setOpen(true)}
                disabled={noAccounts}
              >
                New run
              </Button>
            }
          />
        }
      >
        {(rows) => (
          <div className={styles.list}>
            {rows.map((run) => (
              <RunRow key={run.id} run={run} />
            ))}
          </div>
        )}
      </DataState>

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title="New AI run"
        description="Pick a goal and the account to target. The plan runs immediately and stops at the approval gate before anything is sent."
        footer={
          <>
            <Button variant="secondary" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              form="new-run-form"
              type="submit"
              loading={submitting}
              disabled={!accountId}
            >
              Start run
            </Button>
          </>
        }
      >
        <form id="new-run-form" className={styles.form} onSubmit={onSubmit} noValidate>
          <Field label="Goal" required>
            <Select
              value={goal}
              onChange={(e) => setGoal(e.target.value as RunGoal)}
              options={GOAL_OPTIONS}
            />
          </Field>
          <p className={styles.goalHint}>{GOAL_DESCRIPTION[goal]}</p>

          <Field
            label="Account"
            required
            hint={noAccounts ? "No accounts yet — add one from the Accounts page." : undefined}
          >
            <Select
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
              options={accountOptions}
              placeholder="Select an account"
              disabled={noAccounts}
            />
          </Field>

          {goal === "research_account" && (
            <Field label="Sequence" hint="Where the approved message gets enrolled. Defaults to ai-orchestrated-outbound.">
              <Input
                value={sequence}
                onChange={(e) => setSequence(e.target.value)}
                placeholder="ai-orchestrated-outbound"
              />
            </Field>
          )}
        </form>
      </Modal>
    </div>
  );
}

function RunRow({ run }: { run: Run }) {
  // The list endpoint does not carry each run's full step list — one "3/5" label is not worth
  // shipping every step's output blob — so it sends the counts instead. Reading `steps.length`
  // here made every finished run render "0/0 steps". Falls back to the array for the detail view
  // and for any caller that does populate it.
  const total = run.step_total ?? run.steps.length;
  const done = run.step_done ?? run.steps.filter((s) => s.status === "completed").length;
  const composite = run.blackboard?.composite;

  return (
    <Card padding="lg" interactive className={styles.row}>
      <Link to={`/runs/${run.id}`} className={styles.rowLink} aria-label={`Open run ${run.id}`} />
      <div className={styles.rowMain}>
        <div className={styles.rowTop}>
          <h3 className={styles.goal}>{goalLabel(run.goal)}</h3>
          <Badge tone={RUN_STATUS_TONE[run.status]} dot>
            {RUN_STATUS_LABEL[run.status]}
          </Badge>
        </div>
        <div className={styles.meta}>
          <span>
            {done}/{total} steps
          </span>
          {typeof composite === "number" && <span>Fit {Math.round(composite)}</span>}
          <span>{timeAgo(run.created_at)}</span>
        </div>
      </div>
      <Icons.ChevronRightIcon className={styles.chevron} aria-hidden="true" />
    </Card>
  );
}
