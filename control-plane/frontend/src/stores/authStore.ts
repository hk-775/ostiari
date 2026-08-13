import { create } from "zustand";
import { API_BASE } from "../lib/api";

const TOKEN_KEY = "ostiari_token";

export type Role = "admin" | "operator" | "viewer";

export interface User {
  id: string;
  email: string;
  name: string;
  role: Role;
}

interface AuthState {
  token: string | null;
  user: User | null;
  isAuthenticated: boolean;
  login: (email: string, password: string) => Promise<void>;
  completeSSO: (token: string) => Promise<void>;
  logout: () => void;
  hasRole: (role: Role) => boolean;
  fetchMe: () => Promise<void>;
}

export const useAuthStore = create<AuthState>((set, get) => ({
  token: localStorage.getItem(TOKEN_KEY),
  user: null,
  isAuthenticated: !!localStorage.getItem(TOKEN_KEY),

  login: async (email: string, password: string) => {
    const res = await fetch(`${API_BASE}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: "Login failed" }));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    localStorage.setItem(TOKEN_KEY, data.access_token);
    set({ token: data.access_token, user: data.user, isAuthenticated: true });
  },

  completeSSO: async (token: string) => {
    if (!token) {
      throw new Error("Single sign-on did not return an access token");
    }

    // Validate the callback token before making it the active browser session.
    const res = await fetch(`${API_BASE}/api/auth/me`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) {
      localStorage.removeItem(TOKEN_KEY);
      set({ token: null, user: null, isAuthenticated: false });
      const err = await res.json().catch(() => ({ detail: "Unable to validate SSO session" }));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }

    const user = await res.json();
    localStorage.setItem(TOKEN_KEY, token);
    set({ token, user, isAuthenticated: true });
  },

  logout: () => {
    localStorage.removeItem(TOKEN_KEY);
    set({ token: null, user: null, isAuthenticated: false });
  },

  hasRole: (role: Role) => {
    const user = get().user;
    return user?.role === role;
  },

  fetchMe: async () => {
    const token = get().token;
    if (!token) return;
    const res = await fetch(`${API_BASE}/api/auth/me`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) {
      // Token invalid — log out
      localStorage.removeItem(TOKEN_KEY);
      set({ token: null, user: null, isAuthenticated: false });
      return;
    }
    const user = await res.json();
    set({ user, isAuthenticated: true });
  },
}));
