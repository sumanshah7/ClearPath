"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { BrandMark } from "@/components/pa/BrandMark";

const LINKS = [
  { href: "/doctor/login", label: "Doctor sign in" },
  { href: "/doctor/upload", label: "Upload report" },
  { href: "/doctor", label: "Order desk" },
  { href: "/admin/policies", label: "Policy library" },
  { href: "/insurer", label: "Insurer queue" },
];

export function AppSidebar() {
  const pathname = usePathname();
  if (pathname === "/") return null;

  return (
    <aside className="sidebar">
      <Link href="/" className="brand">
        <BrandMark size={42} />
        ClearPath
      </Link>
      <nav className="sidebar-nav" aria-label="Main">
        {LINKS.map((link) => {
          const active = pathname === link.href || pathname.startsWith(`${link.href}/`);
          return (
            <Link key={link.href} href={link.href} className={active ? "active" : undefined}>
              {link.label}
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}
