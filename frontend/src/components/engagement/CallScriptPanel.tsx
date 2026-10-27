import { useRef, useState } from "react";
import { Button, Card, Icons, Skeleton } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { CampaignCallScript } from "@/lib/types";
import styles from "./CallScriptPanel.module.css";

/**
 * The team's own call script for this campaign (D10, 2026-10-26): uploaded as Word or text, read out
 * in the call console exactly as written, with the AI brief beside it. Enhance asks the AI for a
 * revision that waits here until someone accepts or rejects it; the original is kept.
 */
export function CallScriptPanel({ campaignId }: { campaignId: string }) {
  const api = useApiClient();
  const toast = useToast();
  const script = useApi<CampaignCallScript>((s) => api.campaignCallScript(campaignId, s),
                                            [campaignId]);
  const fileRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState<"upload" | "enhance" | "accept" | "reject" | "remove" | null>(
    null);

  async function run(kind: NonNullable<typeof busy>, work: () => Promise<CampaignCallScript | null>,
                     done: string) {
    setBusy(kind);
    try {
      const next = await work();
      if (next) script.setData(next);
      else script.refetch();
      toast.success(done);
    } catch (err) {
      toast.error("That didn't work", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(null);
    }
  }

  function pick(file: File | undefined) {
    if (!file) return;
    void run("upload", () => api.uploadCampaignCallScript(campaignId, file), "Script saved");
    if (fileRef.current) fileRef.current.value = "";
  }

  const data = script.data;
  return (
    <Card padding="lg" className={styles.panel}>
      <div className={styles.head}>
        <div>
          <h2 className={styles.title}>Call script</h2>
          <p className={styles.lead}>
            Your own script for this campaign's calls, shown in the call console exactly as
            written. Mark what to fill per call: {"{{first_name}}"}, {"{{company}}"},{" "}
            {"{{title}}"}, {"{{my_name}}"} and {"{{trigger}}"} (their strongest recent signal).
          </p>
        </div>
        <div className={styles.actions}>
          <input
            ref={fileRef}
            type="file"
            accept=".docx,.txt,text/plain,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            className={styles.file}
            aria-label="Upload a call script"
            onChange={(e) => pick(e.target.files?.[0])}
          />
          <Button
            variant="secondary"
            iconLeft={<Icons.UploadIcon />}
            loading={busy === "upload"}
            disabled={!!busy}
            onClick={() => fileRef.current?.click()}
          >
            {data?.text ? "Replace" : "Upload script"}
          </Button>
          {data?.text && (
            <Button
              variant="secondary"
              iconLeft={<Icons.BoltIcon />}
              loading={busy === "enhance"}
              disabled={!!busy || !!data.suggestion}
              onClick={() => run("enhance", () => api.enhanceCampaignCallScript(campaignId),
                                 "Suggestion ready below")}
            >
              Enhance with AI
            </Button>
          )}
        </div>
      </div>

      {script.loading && !data ? (
        <Skeleton width="100%" height={120} />
      ) : !data?.text ? (
        <p className={styles.empty}>
          No script yet: calls use the AI talk track. Upload Word (.docx) or text (.txt).
        </p>
      ) : (
        <>
          <p className={styles.meta}>
            {data.filename ?? "Uploaded script"}
            {data.uploaded_at && ` · ${new Date(data.uploaded_at).toLocaleDateString()}`}
            {data.original && " · enhanced (original kept)"}
          </p>
          <div className={data.suggestion ? styles.compare : undefined}>
            <section aria-label="The script as written">
              {data.suggestion && <h3 className={styles.subhead}>As written</h3>}
              <pre className={styles.script}>{data.text}</pre>
            </section>
            {data.suggestion && (
              <section aria-label="Suggested revision">
                <h3 className={styles.subhead}>Suggested revision</h3>
                <pre className={styles.script}>{data.suggestion}</pre>
                <div className={styles.decide}>
                  <Button
                    size="sm"
                    loading={busy === "accept"}
                    disabled={!!busy}
                    onClick={() => run("accept",
                      () => api.decideCallScriptSuggestion(campaignId, "accept"), "Script updated")}
                  >
                    Use this version
                  </Button>
                  <Button
                    size="sm"
                    variant="secondary"
                    loading={busy === "reject"}
                    disabled={!!busy}
                    onClick={() => run("reject",
                      () => api.decideCallScriptSuggestion(campaignId, "reject"),
                      "Kept your script")}
                  >
                    Keep mine
                  </Button>
                </div>
              </section>
            )}
          </div>
          <div className={styles.footer}>
            <Button
              size="sm"
              variant="ghost"
              loading={busy === "remove"}
              disabled={!!busy}
              onClick={() => run("remove", async () => {
                await api.removeCampaignCallScript(campaignId);
                return null;
              }, "Script removed: calls use the AI talk track")}
            >
              Remove script
            </Button>
          </div>
        </>
      )}
    </Card>
  );
}
