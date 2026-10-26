import { Badge } from "@/components/ui";
import { MANDATORY_LABELS } from "@/lib/types";
import type { IcpGate } from "@/lib/types";

function fitTone(score: number): "success" | "warning" | "danger" {
  if (score >= 70) return "success";
  if (score >= 40) return "warning";
  return "danger";
}

/** The short form of a gate that did not pass: "Fails Industry", "Fails Industry +1",
 *  "No Tech stack data". The full sentence rides on the title. */
export function gateLabel(gate: IcpGate): string {
  if (gate.failed.length) {
    const more = gate.failed.length + gate.unknown.length - 1;
    return `Fails ${MANDATORY_LABELS[gate.failed[0]]}${more > 0 ? ` +${more}` : ""}`;
  }
  const more = gate.unknown.length - 1;
  return `No ${MANDATORY_LABELS[gate.unknown[0]]} data${more > 0 ? ` +${more}` : ""}`;
}

/** An account that misses a mandatory field shows which one, never a percentage: a 65% beside an
 *  industry the campaign excludes read as a good match (SDR feedback, 2026-10-26). */
export function FitBadge({ score, gate }: { score?: number | null; gate?: IcpGate | null }) {
  if (gate && !gate.passed) {
    const reason = `${gate.reason}. Outside your must-match fields.`;
    return (
      <Badge tone={gate.failed.length ? "danger" : "warning"} dot title={reason} aria-label={reason}>
        {gateLabel(gate)}
      </Badge>
    );
  }
  if (score == null) return null;
  return <Badge tone={fitTone(score)}>{score}</Badge>;
}

/** For sorting a Fit column: an account outside the must-match fields sorts below every score. */
export function fitSortValue(score?: number | null, gate?: IcpGate | null): number | null {
  if (gate && !gate.passed) return -1;
  return score ?? null;
}
