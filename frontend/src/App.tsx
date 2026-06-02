import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { lazy, Suspense } from "react";
import type { ComponentType, ReactNode } from "react";
import { ThemeProvider } from "@/app/ThemeContext";
import { AuthProvider, useAuth } from "@/app/AuthContext";
import { ToastProvider } from "@/components/ui";
import { AppShell } from "@/components/layout/AppShell";
import { RouteFallback } from "@/components/layout/RouteFallback";
import { LoginPage } from "@/pages/LoginPage";
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
) => lazy(() => load().then((m) => ({ default: m[key] })));

const DashboardPage = lazyPage(() => import("@/pages/DashboardPage"), "DashboardPage");
const InboxPage = lazyPage(() => import("@/pages/InboxPage"), "InboxPage");
const AccountsPage = lazyPage(() => import("@/pages/AccountsPage"), "AccountsPage");
const AccountDetailPage = lazyPage(() => import("@/pages/AccountDetailPage"), "AccountDetailPage");
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
                <Route path="/lists" element={<ListsPage />} />
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
