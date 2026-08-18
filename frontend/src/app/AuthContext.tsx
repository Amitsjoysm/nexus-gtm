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
import type {
  ImpersonationSession,
  LoginRequest,
  NewWorkspaceRequest,
  RegisterStartResponse,
  Role,
  SignupRequest,
} from "@/lib/types";

interface Session {
  token: string;
  role: Role;
  tenantId: string;
  /** Set only while impersonating: the session is read-only and every mutation 403s server-side. */
  readOnly?: boolean;
  /** Email of the user being impersonated, for the banner. */
  impersonating?: string;
}

interface AuthApi {
  /** Configured API client (auth header kept in sync with the session). */
  api: ApiClient;
  session: Session | null;
  isAuthed: boolean;
  login: (body: LoginRequest) => Promise<void>;
  signup: (body: SignupRequest) => Promise<void>;
  /** Step 1 of OTP registration: emails a code, returns the start metadata (no session yet). */
  registerStart: (body: SignupRequest) => Promise<RegisterStartResponse>;
  /** Re-send the verification code for an in-flight registration. */
  registerResend: (email: string) => Promise<RegisterStartResponse>;
  /** Step 2 of OTP registration: verify the code and establish the session. */
  registerVerify: (email: string, code: string) => Promise<void>;
  /** Request a password-reset email (generic response). */
  forgotPassword: (email: string) => Promise<void>;
  /** Complete a password reset with the emailed token. */
  resetPassword: (email: string, token: string, newPassword: string) => Promise<void>;
  logout: () => void;
  /** Re-issue a JWT for another tenant the user belongs to, then swap + refetch. */
  switchTenant: (tenantId: string) => Promise<void>;
  /** Create a new workspace (tenant) owned by the user and switch into it. */
  createWorkspace: (body: NewWorkspaceRequest) => Promise<void>;
  /**
   * Adopt a read-only impersonation session, remembering the admin's own so it can be handed back.
   *
   * The admin session is stashed rather than discarded: ending impersonation must return staff to
   * the console they came from, not to the login screen. Logging them out would make every
   * support look-up cost a re-authentication, which is how a safety feature stops being used.
   */
  beginImpersonation: (session: ImpersonationSession) => void;
  /** Hand back the admin's own session. No-op if not impersonating. */
  endImpersonation: () => void;
  /** Increments on every tenant switch so screens can key off it to remount/refetch. */
  tenantEpoch: number;
}

const STORAGE_KEY = "nexus_session";
// The admin's own session, parked while they impersonate. Separate key so a crashed tab or a
// reload cannot strand staff inside a customer account with no way back.
const ADMIN_STORAGE_KEY = "nexus_admin_session";
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

  const registerStart = useCallback((body: SignupRequest) => api.registerStart(body), [api]);
  const registerResend = useCallback((email: string) => api.registerResend({ email }), [api]);
  const registerVerify = useCallback(
    async (email: string, code: string) => {
      const res = await api.registerVerify({ email, code });
      setSession({ token: res.access_token, role: res.role, tenantId: res.tenant_id });
    },
    [api],
  );

  const forgotPassword = useCallback(
    async (email: string) => {
      await api.forgotPassword({ email });
    },
    [api],
  );
  const resetPassword = useCallback(
    async (email: string, token: string, newPassword: string) => {
      await api.resetPassword({ email, token, new_password: newPassword });
    },
    [api],
  );

  const beginImpersonation = useCallback(
    (imp: ImpersonationSession) => {
      const current = loadSession();
      if (current && !current.readOnly) {
        try {
          localStorage.setItem(ADMIN_STORAGE_KEY, JSON.stringify(current));
        } catch {
          /* storage unavailable — impersonation still works, the return trip is a re-login */
        }
      }
      setSession({
        token: imp.access_token,
        role: "rep" as Role,
        tenantId: imp.tenant_id,
        readOnly: true,
        impersonating: imp.impersonating,
      });
      setTenantEpoch((e) => e + 1);
    },
    [],
  );

  const endImpersonation = useCallback(() => {
    let admin: Session | null = null;
    try {
      const raw = localStorage.getItem(ADMIN_STORAGE_KEY);
      admin = raw ? (JSON.parse(raw) as Session) : null;
      localStorage.removeItem(ADMIN_STORAGE_KEY);
    } catch {
      admin = null;
    }
    // No stashed admin session (storage cleared, or a reload after the admin token expired) means
    // the honest outcome is the login screen, not a silent half-state.
    setSession(admin && admin.token ? admin : null);
    setTenantEpoch((e) => e + 1);
  }, []);

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
    () => ({
      api, session, isAuthed: !!session, login, signup,
      registerStart, registerResend, registerVerify,
      forgotPassword, resetPassword,
      logout, switchTenant, createWorkspace, beginImpersonation, endImpersonation, tenantEpoch,
    }),
    [api, session, login, signup, registerStart, registerResend, registerVerify,
     forgotPassword, resetPassword,
     logout, switchTenant, createWorkspace, tenantEpoch],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
