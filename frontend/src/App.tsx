import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { lazy, Suspense } from "react";
import type { ComponentType, ReactNode } from "react";
import { ThemeProvider } from "@/app/ThemeContext";
import { SignalWindowProvider } from "@/app/SignalWindowContext";
import { AuthProvider, useAuth } from "@/app/AuthContext";
import { isLocked, switchNotice, useEntitlements } from "@/app/EntitlementsContext";
import { FeatureUnavailable } from "@/components/FeatureUnavailable";
import { RequirePlatformAdmin } from "@/app/RequirePlatformAdmin";
import { ToastProvider } from "@/components/ui";
import { AppShell } from "@/components/layout/AppShell";
import { RouteFallback } from "@/components/layout/RouteFallback";
import { LoginPage } from "@/pages/LoginPage";
import { ResetPasswordPage } from "@/pages/ResetPasswordPage";
import type { Role } from "@/lib/types";

/**
 * Route-level code splitting. Each screen ships as its own chunk so the initial
 * load only pays for the shell + the landing route, not all 12 pages. Pages are
 * named exports, so map `.then` to a default for React.lazy.
 *
 * LoginPage stays eager: it's the cold-start entry for unauthenticated users and
 * we never want a loading flash on the very first paint.
 */
const lazyPage = <K extends string>(
  load: () => Promise<Record<K, ComponentType>>,
  key: K,
) =>
  lazy(() =>
    load()
      .then((m) => ({ default: m[key] }))
      .catch((err) => {
        // A deploy can strand a stale shell pointing at dead chunk URLs. One forced
        // reload fetches the fresh shell; the sessionStorage guard prevents loops.
        if (!sessionStorage.getItem("nexus_chunk_reload")) {
          sessionStorage.setItem("nexus_chunk_reload", "1");
          window.location.reload();
        }
        throw err;
      }),
  );

const DashboardPage = lazyPage(() => import("@/pages/DashboardPage"), "DashboardPage");
const InboxPage = lazyPage(() => import("@/pages/InboxPage"), "InboxPage");
const AccountsPage = lazyPage(() => import("@/pages/AccountsPage"), "AccountsPage");
const AccountDetailPage = lazyPage(() => import("@/pages/AccountDetailPage"), "AccountDetailPage");
const ContactsPage = lazyPage(() => import("@/pages/ContactsPage"), "ContactsPage");
const NetworkPage = lazyPage(() => import("@/pages/NetworkPage"), "NetworkPage");
const CallsPage = lazyPage(() => import("@/pages/CallsPage"), "CallsPage");
const SignalsPage = lazyPage(() => import("@/pages/SignalsPage"), "SignalsPage");
const AlertsPage = lazyPage(() => import("@/pages/AlertsPage"), "AlertsPage");
const ListsPage = lazyPage(() => import("@/pages/ListsPage"), "ListsPage");
const PlaysPage = lazyPage(() => import("@/pages/PlaysPage"), "PlaysPage");
const RelevancePage = lazyPage(() => import("@/pages/RelevancePage"), "RelevancePage");
const IntegrationsPage = lazyPage(() => import("@/pages/IntegrationsPage"), "IntegrationsPage");
const MembersPage = lazyPage(() => import("@/pages/MembersPage"), "MembersPage");
const RunsPage = lazyPage(() => import("@/pages/RunsPage"), "RunsPage");
const RunDetailPage = lazyPage(() => import("@/pages/RunDetailPage"), "RunDetailPage");
const ApprovalsPage = lazyPage(() => import("@/pages/ApprovalsPage"), "ApprovalsPage");
const ChatPage = lazyPage(() => import("@/pages/ChatPage"), "ChatPage");
const CampaignsPage = lazyPage(() => import("@/pages/CampaignsPage"), "CampaignsPage");
const CadencesPage = lazyPage(() => import("@/pages/CadencesPage"), "CadencesPage");
const SettingsPage = lazyPage(() => import("@/pages/SettingsPage"), "SettingsPage");
const BillingPage = lazyPage(() => import("@/pages/BillingPage"), "BillingPage");
const AdminHealthPage = lazyPage(
  () => import("@/pages/AdminHealthPage"),
  "AdminHealthPage",
);
const AdminBillingPage = lazyPage(
  () => import("@/pages/AdminBillingPage"),
  "AdminBillingPage",
);

const ROLE_RANK: Record<Role, number> = { owner: 3, admin: 2, manager: 1, rep: 0 };

function RequireAuth({ children }: { children: ReactNode }) {
  const { isAuthed } = useAuth();
  return isAuthed ? <>{children}</> : <Navigate to="/login" replace />;
}

function RequireAnon({ children }: { children: ReactNode }) {
  const { isAuthed } = useAuth();
  return isAuthed ? <Navigate to="/dashboard" replace /> : <>{children}</>;
}

/** Gate a route by minimum role; redirect users who lack it back to the dashboard. */
function RequireRole({ minRole, children }: { minRole: Role; children: ReactNode }) {
  const { session } = useAuth();
  const ok = session ? ROLE_RANK[session.role] >= ROLE_RANK[minRole] : false;
  return ok ? <>{children}</> : <Navigate to="/dashboard" replace />;
}

/**
 * Gate a route by plan entitlement.
 *
 * Hiding a nav link is presentation, not access control: before this existed, a workspace whose
 * plan excluded Campaigns saw no Campaigns link and reached the page perfectly by typing the URL,
 * following a bookmark, or being sent one by a colleague on a richer plan. The whole point of
 * selling a restricted plan is that the restricted parts are not there.
 *
 * Sends the user to /settings/billing rather than /dashboard, matching what a locked nav item
 * does. Redirecting to the dashboard would say "that page does not exist for you" when the honest
 * answer is "your plan does not include it, and here is where you change that".
 *
 * For a `rep` or `manager` that target is itself gated (`/settings/billing` is admin-only), so the
 * chain is /signals -> /settings/billing -> /dashboard. Verified against the running app: it
 * terminates on Dashboard rather than looping, because both hops use `replace`. Reps never see the
 * link in the first place — `navState` hides a locked item from anyone who cannot change the plan —
 * so this only fires on a typed URL or a bookmark shared by a colleague.
 *
 * `isLocked` fails OPEN — no entitlements loaded, request in flight, or enforcement in shadow mode
 * all resolve to "not locked" — so this can never strand someone on a page they are entitled to.
 * The server is still the authority; every one of these routes calls endpoints that meter and 402
 * on their own.
 */
function RequireCapability({
  capability,
  name,
  children,
}: {
  capability: string;
  /** What to call this page when it is unavailable. */
  name: string;
  children: ReactNode;
}) {
  const entitlements = useEntitlements();
  if (!isLocked(entitlements, capability)) return <>{children}</>;

  // A PLATFORM SWITCH AND A PLAN GATE NEED OPPOSITE SCREENS, and telling them apart is the whole
  // reason the server reports `switch_state` separately from `included`.
  //
  // A plan gate is an upsell: the customer can buy their way in, so billing is a real destination.
  // A switch is our decision — no plan re-enables it — so redirecting there would send someone to
  // a checkout page to fix our own maintenance window. It renders in place instead, which also
  // keeps the URL they were sent, so a refresh once the switch is flipped back lands them where
  // they were going.
  const notice = switchNotice(entitlements, capability);
  if (notice) {
    return (
      <FeatureUnavailable
        name={name}
        state={notice.state as Exclude<typeof notice.state, "enabled">}
        message={notice.message}
      />
    );
  }
  return <Navigate to="/settings/billing" replace />;
}

export function App() {
  return (
    <ThemeProvider>
      <SignalWindowProvider>
      <AuthProvider>
        <ToastProvider>
          <BrowserRouter>
            <Suspense fallback={<RouteFallback />}>
            <Routes>
              <Route
                path="/login"
                element={
                  <RequireAnon>
                    <LoginPage />
                  </RequireAnon>
                }
              />

              {/* Public: a password-reset link works whether or not the user is signed in. */}
              <Route path="/reset-password" element={<ResetPasswordPage />} />

              <Route
                element={
                  <RequireAuth>
                    <AppShell />
                  </RequireAuth>
                }
              >
                {/* Dashboard, Accounts and Contacts are ungated on purpose — see NAV_ITEMS. */}
                <Route path="/dashboard" element={<DashboardPage />} />
                <Route
                  path="/inbox"
                  element={
                    <RequireCapability capability="module.signals" name="Inbox">
                      <InboxPage />
                    </RequireCapability>
                  }
                />
                <Route path="/accounts" element={<AccountsPage />} />
                <Route path="/accounts/:id" element={<AccountDetailPage />} />
                <Route path="/contacts" element={<ContactsPage />} />
                <Route
                  path="/network"
                  element={
                    <RequireCapability capability="module.network" name="Network">
                      <NetworkPage />
                    </RequireCapability>
                  }
                />
                <Route
                  path="/calls"
                  element={
                    <RequireCapability capability="module.calling" name="Calls">
                      <CallsPage />
                    </RequireCapability>
                  }
                />
                <Route
                  path="/lists"
                  element={
                    <RequireCapability capability="module.lists" name="Lists">
                      <ListsPage />
                    </RequireCapability>
                  }
                />
                <Route
                  path="/orchestrator"
                  element={
                    <RequireRole minRole="manager">
                      <RequireCapability capability="module.agents" name="Orchestrator">
                        <ChatPage />
                      </RequireCapability>
                    </RequireRole>
                  }
                />
                <Route
                  path="/orchestrator/:sessionId"
                  element={
                    <RequireRole minRole="manager">
                      <RequireCapability capability="module.agents" name="Orchestrator">
                        <ChatPage />
                      </RequireCapability>
                    </RequireRole>
                  }
                />
                <Route
                  path="/signals"
                  element={
                    <RequireCapability capability="module.signals" name="Signals">
                      <SignalsPage />
                    </RequireCapability>
                  }
                />
                <Route
                  path="/alerts"
                  element={
                    <RequireCapability capability="module.signals" name="Alerts">
                      <AlertsPage />
                    </RequireCapability>
                  }
                />
                <Route
                  path="/runs"
                  element={
                    <RequireRole minRole="manager">
                      <RequireCapability capability="module.agents" name="AI Runs">
                        <RunsPage />
                      </RequireCapability>
                    </RequireRole>
                  }
                />
                <Route
                  path="/runs/:id"
                  element={
                    <RequireRole minRole="manager">
                      <RequireCapability capability="module.agents" name="AI Runs">
                        <RunDetailPage />
                      </RequireCapability>
                    </RequireRole>
                  }
                />
                <Route
                  path="/approvals"
                  element={
                    <RequireRole minRole="manager">
                      <RequireCapability capability="module.agents" name="Approvals">
                        <ApprovalsPage />
                      </RequireCapability>
                    </RequireRole>
                  }
                />
                <Route
                  path="/campaigns"
                  element={
                    <RequireRole minRole="manager">
                      <RequireCapability capability="module.outreach" name="Campaigns">
                        <CampaignsPage />
                      </RequireCapability>
                    </RequireRole>
                  }
                />
                <Route
                  path="/cadences"
                  element={
                    <RequireRole minRole="manager">
                      <RequireCapability capability="module.outreach" name="Campaigns">
                        <CadencesPage />
                      </RequireCapability>
                    </RequireRole>
                  }
                />
                <Route
                  path="/plays"
                  element={
                    <RequireRole minRole="manager">
                      <RequireCapability capability="module.plays" name="Plays">
                        <PlaysPage />
                      </RequireCapability>
                    </RequireRole>
                  }
                />
                <Route
                  path="/relevance"
                  element={
                    <RequireRole minRole="manager">
                      <RequireCapability capability="module.relevance" name="Relevance">
                        <RelevancePage />
                      </RequireCapability>
                    </RequireRole>
                  }
                />
                <Route
                  path="/integrations"
                  element={
                    <RequireRole minRole="admin">
                      <RequireCapability capability="module.integrations" name="Integrations">
                        <IntegrationsPage />
                      </RequireCapability>
                    </RequireRole>
                  }
                />
                <Route
                  path="/settings"
                  element={
                    <RequireRole minRole="admin">
                      <SettingsPage />
                    </RequireRole>
                  }
                />
                <Route
                  path="/settings/billing"
                  element={
                    <RequireRole minRole="admin">
                      <BillingPage />
                    </RequireRole>
                  }
                />
                {/* Platform-admin only, and NOT a tenant role — guarding this on minRole="owner"
                    let any workspace owner load the console shell. The API gate
                    (`require_platform_admin`) is still the real boundary; this stops the SPA
                    offering a door that will not open. */}
                <Route
                  path="/admin/billing"
                  element={
                    <RequirePlatformAdmin>
                      <AdminBillingPage />
                    </RequirePlatformAdmin>
                  }
                />
                <Route
                  path="/admin/health"
                  element={
                    <RequirePlatformAdmin>
                      <AdminHealthPage />
                    </RequirePlatformAdmin>
                  }
                />
                <Route path="/members" element={<MembersPage />} />
              </Route>

              <Route path="*" element={<Navigate to="/dashboard" replace />} />
            </Routes>
            </Suspense>
          </BrowserRouter>
        </ToastProvider>
      </AuthProvider>
      </SignalWindowProvider>
    </ThemeProvider>
  );
}
