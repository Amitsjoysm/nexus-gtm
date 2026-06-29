import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { lazy, Suspense } from "react";
import type { ComponentType, ReactNode } from "react";
import { ThemeProvider } from "@/app/ThemeContext";
import { AuthProvider, useAuth } from "@/app/AuthContext";
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

export function App() {
  return (
    <ThemeProvider>
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
                <Route path="/dashboard" element={<DashboardPage />} />
                <Route path="/inbox" element={<InboxPage />} />
                <Route path="/accounts" element={<AccountsPage />} />
                <Route path="/accounts/:id" element={<AccountDetailPage />} />
                <Route path="/contacts" element={<ContactsPage />} />
                <Route path="/calls" element={<CallsPage />} />
                <Route path="/lists" element={<ListsPage />} />
                <Route
                  path="/orchestrator"
                  element={
                    <RequireRole minRole="manager">
                      <ChatPage />
                    </RequireRole>
                  }
                />
                <Route
                  path="/orchestrator/:sessionId"
                  element={
                    <RequireRole minRole="manager">
                      <ChatPage />
                    </RequireRole>
                  }
                />
                <Route path="/signals" element={<SignalsPage />} />
                <Route path="/alerts" element={<AlertsPage />} />
                <Route
                  path="/runs"
                  element={
                    <RequireRole minRole="manager">
                      <RunsPage />
                    </RequireRole>
                  }
                />
                <Route
                  path="/runs/:id"
                  element={
                    <RequireRole minRole="manager">
                      <RunDetailPage />
                    </RequireRole>
                  }
                />
                <Route
                  path="/approvals"
                  element={
                    <RequireRole minRole="manager">
                      <ApprovalsPage />
                    </RequireRole>
                  }
                />
                <Route
                  path="/campaigns"
                  element={
                    <RequireRole minRole="manager">
                      <CampaignsPage />
                    </RequireRole>
                  }
                />
                <Route
                  path="/cadences"
                  element={
                    <RequireRole minRole="manager">
                      <CadencesPage />
                    </RequireRole>
                  }
                />
                <Route
                  path="/plays"
                  element={
                    <RequireRole minRole="manager">
                      <PlaysPage />
                    </RequireRole>
                  }
                />
                <Route
                  path="/relevance"
                  element={
                    <RequireRole minRole="manager">
                      <RelevancePage />
                    </RequireRole>
                  }
                />
                <Route
                  path="/integrations"
                  element={
                    <RequireRole minRole="admin">
                      <IntegrationsPage />
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
                <Route path="/members" element={<MembersPage />} />
              </Route>

              <Route path="*" element={<Navigate to="/dashboard" replace />} />
            </Routes>
            </Suspense>
          </BrowserRouter>
        </ToastProvider>
      </AuthProvider>
    </ThemeProvider>
  );
}
