import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import { ApiClient } from "@/lib/api";
import type { LoginRequest, NewWorkspaceRequest, Role, SignupRequest } from "@/lib/types";

interface Session {
  token: string;
  role: Role;
  tenantId: string;
}

interface AuthApi {
  /** Configured API client (auth header kept in sync with the session). */
  api: ApiClient;
  session: Session | null;
  isAuthed: boolean;
  login: (body: LoginRequest) => Promise<void>;
  signup: (body: SignupRequest) => Promise<void>;
  logout: () => void;
  /** Re-issue a JWT for another tenant the user belongs to, then swap + refetch. */
  switchTenant: (tenantId: string) => Promise<void>;
  /** Create a new workspace (tenant) owned by the user and switch into it. */
  createWorkspace: (body: NewWorkspaceRequest) => Promise<void>;
  /** Increments on every tenant switch so screens can key off it to remount/refetch. */
  tenantEpoch: number;
}

const STORAGE_KEY = "nexus_session";
const AuthContext = createContext<AuthApi | null>(null);

export function useAuth(): AuthApi {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within <AuthProvider>");
  return ctx;
}

/** Convenience accessor for the API client. */
export function useApiClient(): ApiClient {
  return useAuth().api;
}

function loadSession(): Session | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Session;
    return parsed.token ? parsed : null;
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(loadSession);
  const [tenantEpoch, setTenantEpoch] = useState(0);
  // Ref so the client's 401 handler always sees the latest setter without re-creating the client.
  const setSessionRef = useRef(setSession);
  setSessionRef.current = setSession;

  const api = useMemo(() => {
    const client = new ApiClient("/api", () => {
      // Token rejected/expired: clear session so the app routes back to login.
      localStorage.removeItem(STORAGE_KEY);
      setSessionRef.current(null);
    });
    return client;
  }, []);

  // Keep the Authorization header in lockstep with the session *synchronously during render*,
  // before any child renders or fires a request. A tenant switch remounts the whole app subtree
  // (AppShell keys on tenantEpoch); child effects run before a parent's effect, so syncing the
  // token in an effect would let the remounted page fetch with the previous tenant's token and
  // show its data under the new workspace. setToken is idempotent, so calling it every render is
  // cheap and safe (it sets a field, not React state).
  api.setToken(session?.token ?? null);

  // Persist the session for reloads (a real side effect, so it stays in an effect).
  useEffect(() => {
    if (session) localStorage.setItem(STORAGE_KEY, JSON.stringify(session));
  }, [session]);

  const login = useCallback(
    async (body: LoginRequest) => {
      const res = await api.login(body);
      setSession({ token: res.access_token, role: res.role, tenantId: res.tenant_id });
    },
    [api],
  );

  const signup = useCallback(
    async (body: SignupRequest) => {
      const res = await api.signup(body);
      setSession({ token: res.access_token, role: res.role, tenantId: res.tenant_id });
    },
    [api],
  );

  const logout = useCallback(() => {
    localStorage.removeItem(STORAGE_KEY);
    setSession(null);
  }, []);

  const switchTenant = useCallback(
    async (tenantId: string) => {
      const res = await api.switchTenant(tenantId);
      setSession({ token: res.access_token, role: res.role, tenantId: res.tenant_id });
      setTenantEpoch((n) => n + 1);
    },
    [api],
  );

  const createWorkspace = useCallback(
    async (body: NewWorkspaceRequest) => {
      const res = await api.createWorkspace(body);
      setSession({ token: res.access_token, role: res.role, tenantId: res.tenant_id });
      setTenantEpoch((n) => n + 1);  // new empty tenant: remount/refetch every screen
    },
    [api],
  );

  const value = useMemo<AuthApi>(
    () => ({ api, session, isAuthed: !!session, login, signup, logout, switchTenant, createWorkspace, tenantEpoch }),
    [api, session, login, signup, logout, switchTenant, createWorkspace, tenantEpoch],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
