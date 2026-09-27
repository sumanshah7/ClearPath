"use client";

import { DoctorAuthProvider } from "@/components/pa/DoctorAuthProvider";

export default function DoctorLayout({ children }: { children: React.ReactNode }) {
  return <DoctorAuthProvider>{children}</DoctorAuthProvider>;
}
