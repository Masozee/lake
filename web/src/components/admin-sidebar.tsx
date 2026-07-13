/**
 * The admin rail.
 *
 * Carbon's UI shell is a dark rail in both themes — the panel is a tool, and the
 * rail is chrome rather than content. Keeping it charcoal on a white page is what
 * makes the working area read as the thing you are working *on*, instead of one
 * more panel among several.
 */

import { Link, useRouterState } from "@tanstack/react-router"
import {
  Activity,
  ChevronUp,
  Database,
  FileCode2,
  HardDrive,
  LayoutDashboard,
  LogOut,
  Moon,
  ScrollText,
  Settings,
  Sun,
  Users,
} from "lucide-react"
import type { LucideIcon } from "lucide-react"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar"
import type { Me } from "@/lib/admin"

type Item = { to: string; label: string; icon: LucideIcon; exact?: boolean }

/** Grouped by the question each one answers, not by which endpoint it calls. */
const MONITOR: Array<Item> = [
  { to: "/admin", label: "Overview", icon: LayoutDashboard, exact: true },
  { to: "/admin/runs", label: "Runs & errors", icon: Activity },
  { to: "/admin/storage", label: "Storage", icon: HardDrive },
]

const DATA: Array<Item> = [{ to: "/admin/data", label: "Data", icon: Database }]

const MANAGE: Array<Item> = [
  { to: "/admin/sources", label: "Sources", icon: FileCode2 },
  { to: "/admin/audit", label: "Audit log", icon: ScrollText },
]

/** `exact` on the index only: every other admin path starts with /admin, so a
    prefix match would light Overview up on all of them. */
function isCurrent(item: Item, path: string): boolean {
  return item.exact ? path === item.to : path.startsWith(item.to)
}

export function AdminSidebar({
  me,
  onSignOut,
}: {
  me: Me
  onSignOut: () => void
}) {
  const path = useRouterState({ select: (s) => s.location.pathname })

  // These are Base UI components, not Radix: the element to render is passed as
  // `render`, and the children come from the button's own props.
  const group = (label: string, items: Array<Item>) => (
    <SidebarGroup>
      <SidebarGroupLabel>{label}</SidebarGroupLabel>
      <SidebarGroupContent>
        <SidebarMenu>
          {items.map((item) => (
            <SidebarMenuItem key={item.to}>
              <SidebarMenuButton
                render={<Link to={item.to} />}
                isActive={isCurrent(item, path)}
                tooltip={item.label}
              >
                <item.icon />
                <span>{item.label}</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          ))}
        </SidebarMenu>
      </SidebarGroupContent>
    </SidebarGroup>
  )

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            {/* Back to the public site. An admin panel with no way out to the thing
                it administers is a cul-de-sac. */}
            <SidebarMenuButton
              render={<Link to="/" search={{}} />}
              size="lg"
              tooltip="Back to the public site"
            >
              <span className="logo" aria-hidden="true" />
              <span className="flex flex-col gap-0.5 leading-none">
                <span className="font-semibold">lake</span>
                <span className="text-xs opacity-70">admin</span>
              </span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      <SidebarContent>
        {group("Monitor", MONITOR)}
        {group("Browse", DATA)}
        {group("Manage", MANAGE)}
      </SidebarContent>

      {/* Profile and settings live at the bottom, where every tool of this shape
          puts them — it is the one piece of muscle memory worth honouring. */}
      <SidebarFooter>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              render={<Link to="/admin/settings" />}
              isActive={path === "/admin/settings"}
              tooltip="Settings"
            >
              <Settings />
              <span>Settings</span>
            </SidebarMenuButton>
          </SidebarMenuItem>

          <SidebarMenuItem>
            <DropdownMenu>
              <DropdownMenuTrigger
                render={
                  <SidebarMenuButton size="lg" tooltip={me.email}>
                    <Avatar name={me.display_name} />
                    <span className="flex min-w-0 flex-col gap-0.5 leading-none">
                      <span className="truncate font-medium">
                        {me.display_name}
                      </span>
                      <span className="truncate text-xs opacity-70">
                        {me.email}
                      </span>
                    </span>
                    <ChevronUp className="ml-auto" />
                  </SidebarMenuButton>
                }
              />
              <DropdownMenuContent side="top" align="start" className="w-56">
                <DropdownMenuItem render={<Link to="/admin/users" />}>
                  <Users />
                  <span>Admins &amp; password</span>
                </DropdownMenuItem>
                <DropdownMenuItem onClick={toggleTheme}>
                  {/* One item, not two: the icon shows the mode you would switch TO,
                      which is the same trick the public header uses. */}
                  <span className="theme-light contents">
                    <Moon />
                    <span>Dark mode</span>
                  </span>
                  <span className="theme-dark contents">
                    <Sun />
                    <span>Light mode</span>
                  </span>
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem onClick={onSignOut}>
                  <LogOut />
                  <span>Sign out</span>
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
    </Sidebar>
  )
}

/** Initials on a square. Carbon does not round, so neither does this. */
function Avatar({ name }: { name: string }) {
  const initials = name
    .split(/\s+/)
    .slice(0, 2)
    .map((w) => w[0] ?? "")
    .join("")
    .toUpperCase()

  return (
    <span
      aria-hidden="true"
      className="flex size-8 shrink-0 items-center justify-center text-xs font-semibold"
      style={{
        background: "var(--sidebar-primary)",
        color: "var(--sidebar-primary-foreground)",
      }}
    >
      {initials}
    </span>
  )
}

function toggleTheme() {
  const dark = document.documentElement.classList.toggle("dark")
  try {
    localStorage.setItem("theme", dark ? "dark" : "light")
  } catch {
    // A reader with storage disabled still gets the toggle, just not the memory.
  }
}
