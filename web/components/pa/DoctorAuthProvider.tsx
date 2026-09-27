"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { usePathname, useRouter } from "next/navigation";

export type DoctorSessionInfo = {
  session_id: string;
  user_id: string;
  provider_id: string;
  display_name: string;
  npi: string;
  full_name: string;
  specialty?: string;
  credentials?: string;
  phone?: string;
  email?: string;
  tax_id?: string;
  clinic_id?: string | null;
  clinic_name?: string | null;
  clinic_phone?: string | null;
  clinic_npi?: string | null;
  clinic_address?: string | null;
  created_at: string;
};

type DoctorAuthState = {
  doctor: DoctorSessionInfo | null;
  loading: boolean;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
};

const DoctorAuthContext = createContext<DoctorAuthState | null>(null);

export function useDoctorAuth(): DoctorAuthState {
  const ctx = useContext(DoctorAuthContext);
  if (!ctx) throw new Error("useDoctorAuth must be used inside DoctorAuthProvider");
  return ctx;
}

export function DoctorAuthProvider({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [doctor, setDoctor] = useState<DoctorSessionInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const isLogin = pathname === "/doctor/login";

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/auth/doctor/me", { cache: "no-store" });
      if (!res.ok) {
        setDoctor(null);
        return;
      }
      const data = await res.json();
      setDoctor(data.doctor || null);
    } catch {
      setDoctor(null);
    } finally {
      setLoading(false);
    }
  }, []);

  const logout = useCallback(async () => {
    await fetch("/api/auth/doctor/logout", { method: "POST" });
    setDoctor(null);
    router.replace("/doctor/login");
  }, [router]);

  useEffect(() => {
    refresh();
  }, [refresh, pathname]);

  useEffect(() => {
    if (loading || isLogin) return;
    if (!doctor) {
      const next = encodeURIComponent(pathname || "/doctor/upload");
      router.replace(`/doctor/login?next=${next}`);
    }
  }, [loading, doctor, isLogin, pathname, router]);

  const value = useMemo(
    () => ({ doctor, loading, refresh, logout }),
    [doctor, loading, refresh, logout],
  );

  if (!isLogin && loading) {
    return (
      <main>
        <p className="muted" style={{ padding: "2rem" }}>
          Checking clinician session...
        </p>
      </main>
    );
  }

  if (!isLogin && !doctor) {
    return (
      <main>
        <p className="muted" style={{ padding: "2rem" }}>
          Redirecting to clinician login...
        </p>
      </main>
    );
  }

  return <DoctorAuthContext.Provider value={value}>{children}</DoctorAuthContext.Provider>;
}
