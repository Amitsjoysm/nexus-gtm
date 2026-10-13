import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Badge, Button, Checkbox, Icons, Skeleton } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { PRODUCT_NAME } from "@/lib/brand";
import { SCOPE_COPY, sortScopes } from "@/components/mcp/scopes";
import type { McpScope, OAuthConsentView } from "@/lib/types";
import styles from "./OAuthConsentPage.module.css";

/**
 * Where an AI app sends someone to approve it (OAuth). Reached from `/oauth/authorize`, which has
 * already checked the app and its redirect, and signed what it asked for into `?request=`.
 *
 * What this screen must make impossible to miss, because anyone can register an app called
 * "Claude": where the browser goes afterwards (the redirect host, in large type), whether the app
 * is verified, and that an app returning to this computer could be any program on it. Then what
 * the app may do, as checkboxes the person can narrow, and that what it reads leaves for the app's
 * AI provider.
 *
 * Outside the app shell: it is a decision, not a page of the product, and the sidebar would only
 * offer ways to wander off half way through someone else's sign-in.
 */
export function OAuthConsentPage() {
  const api = useApiClient();
  const [params] = useSearchParams();
  const request = params.get("request") ?? "";
  const view = useApi<OAuthConsentView>(
    (signal) =>
      request
        ? api.oauthRequest(request, signal)
        : Promise.reject(new ApiError(400, "This sign-in link is incomplete. Start again from the app.")),
    [request],
  );
  const [scopes, setScopes] = useState<McpScope[]>([]);
  const [busy, setBusy] = useState<"allow" | "deny" | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (view.data) setScopes(sortScopes(view.data.scopes));
  }, [view.data]);

  useEffect(() => {
    const name = view.data?.client_name;
    document.title = name ? `Allow ${name} · ${PRODUCT_NAME}` : `Connect an app · ${PRODUCT_NAME}`;
  }, [view.data?.client_name]);

  const decide = async (choice: "allow" | "deny") => {
    setBusy(choice);
    setError("");
    try {
      const res =
        choice === "allow" ? await api.oauthApprove(request, scopes) : await api.oauthDeny(request);
      window.location.assign(res.redirect_to);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Something went wrong. Try again.");
      setBusy(null);
    }
  };

  return (
    <main className={styles.page}>
      <div className={styles.panel}>
        <div className={styles.brand}>
          <img src="/infojoy-mark.png" alt="" className={styles.mark} />
          <span>{PRODUCT_NAME}</span>
        </div>

        {view.loading && !view.data ? (
          <div className={styles.body} aria-busy="true" aria-label="Loading">
            <Skeleton height="2rem" width="80%" />
            <Skeleton height="4.5rem" />
            <Skeleton height="10rem" />
          </div>
        ) : view.error && !view.data ? (
          <div className={styles.body}>
            <h1 className={styles.title}>This sign-in link can't be used</h1>
            <p className={styles.lead}>{view.error.detail}</p>
            <p className={styles.muted}>
              Go back to the app and start connecting again. Links expire after 10 minutes.
            </p>
            <Link to="/dashboard" className={styles.homeLink}>
              Go to {PRODUCT_NAME}
            </Link>
          </div>
        ) : view.data ? (
          <ConsentBody
            view={view.data}
            scopes={scopes}
            onScopes={setScopes}
            busy={busy}
            error={error}
            onDecide={decide}
          />
        ) : null}
      </div>
    </main>
  );
}

function ConsentBody({
  view,
  scopes,
  onScopes,
  busy,
  error,
  onDecide,
}: {
  view: OAuthConsentView;
  scopes: McpScope[];
  onScopes: (next: McpScope[]) => void;
  busy: "allow" | "deny" | null;
  error: string;
  onDecide: (choice: "allow" | "deny") => void;
}) {
  const requested = sortScopes(view.scopes);
  return (
    <div className={styles.body}>
      <div className={styles.identity}>
        {view.logo_uri && view.verified ? (
          <img src={view.logo_uri} alt="" className={styles.appLogo} />
        ) : (
          <span className={styles.appInitial} aria-hidden="true">
            {view.client_name.slice(0, 1).toUpperCase()}
          </span>
        )}
        <div className={styles.identityText}>
          <h1 className={styles.title}>
            Allow {view.client_name} to use your {PRODUCT_NAME} account?
          </h1>
          {view.verified ? (
            <Badge tone="success" icon={<Icons.ShieldCheckIcon />}>
              Verified app
            </Badge>
          ) : (
            <Badge tone="warning" icon={<Icons.AlertTriangleIcon />}>
              Unverified app
            </Badge>
          )}
        </div>
      </div>

      <div className={styles.destination}>
        <span className={styles.destinationLabel}>After you choose, you go back to</span>
        <span className={styles.host}>{view.redirect_host}</span>
      </div>

      {!view.verified && (
        <p className={styles.warning} role="note">
          <Icons.AlertTriangleIcon aria-hidden="true" />
          <span>
            {PRODUCT_NAME} has not verified this app. It calls itself "{view.client_name}", which any
            app can do. Continue only if you started connecting it yourself, just now.
          </span>
        </p>
      )}
      {view.only_loopback && (
        <p className={styles.warning} role="note">
          <Icons.LockIcon aria-hidden="true" />
          <span>
            This app runs on your own computer and returns to {view.redirect_host}. Any program on
            this computer could answer there, so continue only if you just started this from an app
            you trust.
          </span>
        </p>
      )}

      <dl className={styles.account}>
        <div>
          <dt>Workspace</dt>
          <dd>{view.workspace_name || "Your workspace"}</dd>
        </div>
        <div>
          <dt>Signed in as</dt>
          <dd>{view.user_email}</dd>
        </div>
      </dl>

      <fieldset className={styles.scopes}>
        <legend className={styles.scopesLegend}>{view.client_name} will be able to</legend>
        {requested.map((s) => {
          const id = `consent-${s}`;
          return (
            <label key={s} htmlFor={id} className={styles.scope}>
              <Checkbox
                id={id}
                label={SCOPE_COPY[s].label}
                checked={scopes.includes(s)}
                onChange={(e) =>
                  onScopes(
                    e.target.checked ? sortScopes([...scopes, s]) : scopes.filter((x) => x !== s),
                  )
                }
              />
              <span className={styles.scopeText}>
                <span className={styles.scopeName}>{SCOPE_COPY[s].label}</span>
                <span className={styles.scopeDetail}>{SCOPE_COPY[s].detail}</span>
              </span>
            </label>
          );
        })}
      </fieldset>

      <p className={styles.notice}>
        It acts as you, inside this workspace. What it reads is sent to {view.client_name} and the AI
        model behind it. You can change what it may do, or revoke it, at any time in AI apps.
      </p>

      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}

      <div className={styles.actions}>
        <Button
          variant="secondary"
          onClick={() => onDecide("deny")}
          loading={busy === "deny"}
          disabled={busy !== null}
        >
          Cancel
        </Button>
        <Button
          variant="primary"
          onClick={() => onDecide("allow")}
          loading={busy === "allow"}
          disabled={busy !== null || scopes.length === 0}
        >
          Allow access
        </Button>
      </div>
      {scopes.length === 0 && (
        <p className={styles.muted}>Tick at least one permission to allow access.</p>
      )}
    </div>
  );
}

export default OAuthConsentPage;
