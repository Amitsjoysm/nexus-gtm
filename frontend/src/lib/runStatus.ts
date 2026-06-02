/**
 * Presentation vocabulary for orchestration runs — status→tone maps and human labels,
 * shared by the runs list, run console, and approvals queue so a "running" step looks the
 * same everywhere. Color is always paired with a label (and Badge dot), never the sole cue.
 */
import type { BadgeTone } from "@/components/ui";
import type { ApprovalStatus, RunStatus, StepStatus } from "./types";

export const RUN_STATUS_TONE: Record<RunStatus, BadgeTone> = {
  planning: "neutral",
  running: "info",
  awaiting_approval: "warning",
  completed: "success",
  failed: "danger",
  cancelled: "neutral",
};

export const RUN_STATUS_LABEL: Record<RunStatus, string> = {
  planning: "Planning",
  running: "Running",
  awaiting_approval: "Awaiting approval",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

export const STEP_STATUS_TONE: Record<StepStatus, BadgeTone> = {
  pending: "neutral",
  running: "info",
  awaiting_approval: "warning",
  completed: "success",
  failed: "danger",
  skipped: "neutral",
  rejected: "danger",
};

export const STEP_STATUS_LABEL: Record<StepStatus, string> = {
  pending: "Pending",
  running: "Running",
  awaiting_approval: "Awaiting approval",
  completed: "Done",
  failed: "Failed",
  skipped: "Skipped",
  rejected: "Rejected",
};

export const APPROVAL_STATUS_TONE: Record<ApprovalStatus, BadgeTone> = {
  pending: "warning",
  approved: "success",
  rejected: "danger",
};

/** Friendly names for the tools a step can invoke. */
export const TOOL_LABEL: Record<string, string> = {
  research: "Research account",
  scoring: "Score fit",
  compose_message: "Compose outreach",
  send_message: "Send to sequence",
};

export const GOAL_LABEL: Record<string, string> = {
  research_account: "Research & draft outreach",
  research_only: "Research & score",
};

export const GOAL_DESCRIPTION: Record<string, string> = {
  research_account:
    "Research the account, score its fit, draft a personalized message, and park the send for approval.",
  research_only: "Gather grounded facts and compute the fit score. No outbound, no approval gate.",
};

const RUN_TERMINAL = new Set<RunStatus>(["completed", "failed", "cancelled"]);
export const isRunActive = (status: RunStatus): boolean => !RUN_TERMINAL.has(status);

export const toolLabel = (tool: string): string =>
  TOOL_LABEL[tool] ?? tool.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

export const goalLabel = (goal: string): string => GOAL_LABEL[goal] ?? goal;
