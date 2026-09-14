"use client";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, PageHeader } from "@/components/ui/card";
import { listDevices, revokeDevice, updateMe, type Device } from "@/features/auth/api";
import { currentUser, storeSession } from "@/features/auth/session";
export default function SettingsPage() {
  const u = currentUser();
  const [name, setName] = useState(u?.full_name ?? "");
  const [timezone, setTimezone] = useState(
    u?.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone,
  );
  const [devices, setDevices] = useState<Device[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const load = () => void listDevices().then(setDevices);
  useEffect(load, []);
  async function save() {
    const user = await updateMe({ full_name: name, timezone });
    const raw = sessionStorage.getItem("verity.access"),
      ref = sessionStorage.getItem("verity.refresh");
    if (raw && ref)
      storeSession({
        user,
        tokens: {
          access_token: raw,
          refresh_token: ref,
          expires_in: 900,
          token_type: "Bearer",
        },
      });
    setMessage("Profile settings saved.");
  }
  return (
    <div className="space-y-7">
      <PageHeader
        title="Settings"
        description="Profile, security sessions, and privacy controls."
      />
      <Card>
        <CardHeader title="Profile" />
        <CardBody>
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="space-y-1 text-sm">
              <span>Full name</span>
              <input className="field" value={name} onChange={(e) => setName(e.target.value)} />
            </label>
            <label className="space-y-1 text-sm">
              <span>Timezone</span>
              <input
                className="field"
                value={timezone}
                onChange={(e) => setTimezone(e.target.value)}
              />
            </label>
          </div>
          <div className="mt-4">
            <Button onClick={() => void save()}>Save changes</Button>
          </div>
          {message ? (
            <p className="mt-2 text-sm text-[var(--color-positive)]">{message}</p>
          ) : null}
        </CardBody>
      </Card>
      <Card>
        <CardHeader title="Signed-in devices" meta="Revoke any session you do not recognize." />
        <CardBody>
          <ul className="divide-y divide-[var(--color-border-subtle)]">
            {devices.map((d) => (
              <li key={d.id} className="flex items-center justify-between gap-3 py-3">
                <div>
                  <p className="text-sm font-medium">{d.name}</p>
                  <p className="text-xs text-[var(--color-text-muted)]">
                    {d.platform} · {new Date(d.last_seen_at).toLocaleString()}
                  </p>
                </div>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => void revokeDevice(d.id).then(load)}
                >
                  Revoke
                </Button>
              </li>
            ))}
          </ul>
        </CardBody>
      </Card>
      <Card>
        <CardHeader
          title="Privacy & data"
          meta="Export your data or request verified deletion."
          action={
            <Button variant="secondary" href="/account">
              Open privacy controls
            </Button>
          }
        />
      </Card>
    </div>
  );
}
