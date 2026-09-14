/* eslint-disable max-lines */
"use client";

import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/card";
import { Empty, Loading } from "@/components/ui/states";
import {
  activatePrompt,
  createPrompt,
  getAISettings,
  grantStaff,
  listAdminSessions,
  listFlags,
  listPrompts,
  listStaff,
  revokeStaff,
  saveAISettings,
  saveFlag,
  searchUsers,
  type AdminSession,
  type AISettings,
  type FeatureFlag,
  type PromptVersion,
  type StaffGrant,
} from "@/features/admin/api";
import { ApiError } from "@/lib/api/client";

function Message({ children }: { children: string | null }) {
  return children ? (
    <p role="status" className="rounded-lg bg-[var(--color-raised)] p-3 text-sm">
      {children}
    </p>
  ) : null;
}

export function AISettingsPane({ canWrite }: { canWrite: boolean }) {
  const [value, setValue] = useState<AISettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    void getAISettings()
      .then(setValue)
      .catch((error) =>
        setMessage(error instanceof Error ? error.message : "Could not load settings"),
      );
  }, []);

  async function save() {
    if (!value) return;
    setBusy(true);
    try {
      setValue(await saveAISettings(value));
      setMessage("AI routing and limits are active. The change was added to the audit log.");
    } catch (error) {
      setMessage(error instanceof ApiError ? error.message : "Could not save settings");
    } finally {
      setBusy(false);
    }
  }

  if (!value) return message ? <Message>{message}</Message> : <Loading rows={4} />;
  const set = <K extends keyof AISettings>(key: K, next: AISettings[K]) =>
    setValue((current) => (current ? { ...current, [key]: next } : current));

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title="Model gateway"
          meta="Changes take effect across API workers without a restart. Provider keys remain in the server secret store."
          action={
            <Button size="sm" onClick={() => void save()} loading={busy} disabled={!canWrite}>
              Save changes
            </Button>
          }
        />
        <CardBody>
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="Provider">
              <select
                className="field"
                value={value.provider}
                disabled={!canWrite}
                onChange={(event) =>
                  set("provider", event.target.value as AISettings["provider"])
                }
              >
                <option value="groq">Groq</option>
                <option value="anthropic">Anthropic</option>
                <option value="stub">Deterministic local fallback</option>
              </select>
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <ProviderStatus name="Groq" ready={value.groq_key_configured} />
              <ProviderStatus name="Anthropic" ready={value.anthropic_key_configured} />
            </div>
            <TextField
              label="Fast classifier model"
              value={value.fast_model}
              onChange={(x) => set("fast_model", x)}
              disabled={!canWrite}
            />
            <TextField
              label="Realtime guidance model"
              value={value.realtime_model}
              onChange={(x) => set("realtime_model", x)}
              disabled={!canWrite}
            />
            <TextField
              label="Reasoning and report model"
              value={value.reasoning_model}
              onChange={(x) => set("reasoning_model", x)}
              disabled={!canWrite}
            />
            <NumberField
              label="Daily AI budget (USD)"
              value={value.daily_budget_usd}
              min={1}
              step={1}
              onChange={(x) => set("daily_budget_usd", x)}
              disabled={!canWrite}
            />
            <NumberField
              label="Budget warning threshold"
              value={value.budget_soft_threshold}
              min={0.1}
              max={1}
              step={0.05}
              onChange={(x) => set("budget_soft_threshold", x)}
              disabled={!canWrite}
            />
            <NumberField
              label="Generations per session"
              value={value.session_max_generations}
              min={1}
              step={1}
              onChange={(x) => set("session_max_generations", x)}
              disabled={!canWrite}
            />
            <NumberField
              label="Generations per minute"
              value={value.session_max_generations_per_minute}
              min={1}
              step={1}
              onChange={(x) => set("session_max_generations_per_minute", x)}
              disabled={!canWrite}
            />
            <NumberField
              label="Maximum session minutes"
              value={value.session_max_duration_minutes}
              min={5}
              step={5}
              onChange={(x) => set("session_max_duration_minutes", x)}
              disabled={!canWrite}
            />
          </div>
          <div className="mt-4">
            <Message>{message}</Message>
          </div>
        </CardBody>
      </Card>
      <Card>
        <CardHeader
          title="Secret management"
          meta="Secrets cannot be read or changed from a browser session."
        />
        <CardBody>
          <p className="text-sm text-[var(--color-text-secondary)]">
            Configure provider credentials in the deployment secret store. This panel exposes
            only their presence, never their value. Rotate keys at the infrastructure layer,
            then use this page to select the provider and models.
          </p>
        </CardBody>
      </Card>
    </div>
  );
}

function ProviderStatus({ name, ready }: { name: string; ready: boolean }) {
  return (
    <div className="rounded-lg border border-[var(--color-border-subtle)] p-3">
      <p className="text-xs text-[var(--color-text-muted)]">{name} key</p>
      <Badge tone={ready ? "positive" : "warning"}>{ready ? "Configured" : "Missing"}</Badge>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="space-y-1.5 text-sm">
      <span className="font-medium">{label}</span>
      {children}
    </label>
  );
}

function TextField({
  label,
  value,
  onChange,
  disabled,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  disabled: boolean;
}) {
  return (
    <Field label={label}>
      <input
        className="field"
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      />
    </Field>
  );
}

function NumberField({
  label,
  value,
  onChange,
  disabled,
  min,
  max,
  step,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  disabled: boolean;
  min: number;
  max?: number;
  step: number;
}) {
  return (
    <Field label={label}>
      <input
        type="number"
        className="field"
        value={value}
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        onChange={(event) => onChange(Number(event.target.value))}
      />
    </Field>
  );
}

export function FlagsPane({ canWrite }: { canWrite: boolean }) {
  const [flags, setFlags] = useState<FeatureFlag[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  useEffect(() => {
    void listFlags()
      .then(setFlags)
      .catch(() => setMessage("Could not load feature flags"));
  }, []);

  async function persist(flag: FeatureFlag) {
    setBusy(flag.key);
    try {
      const saved = await saveFlag(flag);
      setFlags(
        (current) => current?.map((item) => (item.key === saved.key ? saved : item)) ?? null,
      );
      setMessage(`${flag.key} is active at ${flag.enabled ? flag.rollout_percentage : 0}%.`);
    } catch (error) {
      setMessage(error instanceof ApiError ? error.message : "Could not update flag");
    } finally {
      setBusy(null);
    }
  }

  if (!flags) return message ? <Message>{message}</Message> : <Loading rows={4} />;
  return (
    <div className="space-y-4">
      <Message>{message}</Message>
      {flags.map((flag) => (
        <Card key={flag.key}>
          <CardBody>
            <div className="grid gap-4 md:grid-cols-[1fr_8rem_8rem] md:items-end">
              <div>
                <div className="flex items-center gap-2">
                  <code>{flag.key}</code>
                  <Badge tone={flag.enabled ? "positive" : "neutral"}>
                    {flag.enabled ? "Enabled" : "Disabled"}
                  </Badge>
                </div>
                <p className="mt-2 text-sm text-[var(--color-text-secondary)]">
                  {flag.description}
                </p>
              </div>
              <Field label="Rollout %">
                <input
                  type="number"
                  className="field"
                  min={0}
                  max={100}
                  disabled={!canWrite || !flag.enabled}
                  value={flag.rollout_percentage}
                  onChange={(event) =>
                    setFlags(
                      (current) =>
                        current?.map((item) =>
                          item.key === flag.key
                            ? { ...item, rollout_percentage: Number(event.target.value) }
                            : item,
                        ) ?? null,
                    )
                  }
                />
              </Field>
              <div className="space-y-2">
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={flag.enabled}
                    disabled={!canWrite}
                    onChange={(event) =>
                      setFlags(
                        (current) =>
                          current?.map((item) =>
                            item.key === flag.key
                              ? { ...item, enabled: event.target.checked }
                              : item,
                          ) ?? null,
                      )
                    }
                  />{" "}
                  Enabled
                </label>
                <Button
                  size="sm"
                  variant="secondary"
                  fullWidth
                  loading={busy === flag.key}
                  disabled={!canWrite}
                  onClick={() => void persist(flag)}
                >
                  Apply
                </Button>
              </div>
            </div>
          </CardBody>
        </Card>
      ))}
    </div>
  );
}

export function SessionsPane() {
  const [kind, setKind] = useState("all");
  const [rows, setRows] = useState<AdminSession[] | null>(null);
  const load = useCallback(() => {
    setRows(null);
    void listAdminSessions(kind).then(setRows);
  }, [kind]);
  useEffect(() => {
    load();
  }, [load]);
  return (
    <Card>
      <CardHeader
        title="Interview sessions"
        meta="Operational metadata only. Transcript access is deliberately unavailable without a support case and user-visible audit record."
        action={
          <select
            className="field"
            value={kind}
            onChange={(event) => setKind(event.target.value)}
          >
            <option value="all">All sessions</option>
            <option value="live">Live</option>
            <option value="mock">Mock</option>
          </select>
        }
      />
      <CardBody>
        {!rows ? (
          <Loading rows={5} />
        ) : rows.length === 0 ? (
          <Empty title="No sessions" description="Real candidate sessions will appear here." />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-[var(--color-text-muted)]">
                <tr>
                  <th className="pb-2">Candidate</th>
                  <th>Type</th>
                  <th>Status</th>
                  <th>Mode</th>
                  <th>Created</th>
                  <th className="text-right">Duration</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={`${row.kind}-${row.id}`}
                    className="border-t border-[var(--color-border-subtle)]"
                  >
                    <td className="py-3 pr-4">
                      <p>{row.user_email}</p>
                      <code className="text-[10px] text-[var(--color-text-muted)]">
                        {row.id}
                      </code>
                    </td>
                    <td>
                      <Badge tone={row.kind === "live" ? "accent" : "neutral"}>
                        {row.kind}
                      </Badge>
                    </td>
                    <td>{row.status}</td>
                    <td className="capitalize">{row.mode.replaceAll("_", " ")}</td>
                    <td>{new Date(row.created_at).toLocaleString()}</td>
                    <td className="numeric text-right">
                      {row.duration_seconds == null ? "—" : `${row.duration_seconds}s`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardBody>
      {rows ? <CardFooter>{rows.length} session(s)</CardFooter> : null}
    </Card>
  );
}

export function StaffPane({ canWrite }: { canWrite: boolean }) {
  const [grants, setGrants] = useState<StaffGrant[] | null>(null);
  const [users, setUsers] = useState<Array<{ id: string; email: string }>>([]);
  const [userId, setUserId] = useState("");
  const [role, setRole] = useState("support");
  const [message, setMessage] = useState<string | null>(null);
  const load = useCallback(async () => {
    const [staff, accounts] = await Promise.all([listStaff(), searchUsers("")]);
    setGrants(staff);
    setUsers(accounts);
    if (!userId && accounts[0]) setUserId(accounts[0].id);
  }, [userId]);
  useEffect(() => {
    void load();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  async function grant() {
    try {
      await grantStaff(userId, role);
      setMessage("Staff role granted and audited.");
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not grant role");
    }
  }
  async function revoke(id: string) {
    try {
      await revokeStaff(id);
      setMessage("Staff role revoked and audited.");
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not revoke role");
    }
  }
  if (!grants) return <Loading rows={4} />;
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title="Grant staff access"
          meta="Roles are explicit permission sets. Grant the minimum role needed."
        />
        <CardBody>
          <div className="grid gap-3 md:grid-cols-[1fr_14rem_auto] md:items-end">
            <Field label="Account">
              <select
                className="field"
                value={userId}
                onChange={(event) => setUserId(event.target.value)}
              >
                {users.map((user) => (
                  <option value={user.id} key={user.id}>
                    {user.email}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Role">
              <select
                className="field"
                value={role}
                onChange={(event) => setRole(event.target.value)}
              >
                {["support", "billing_ops", "ai_ops", "security", "admin"].map((item) => (
                  <option key={item}>{item}</option>
                ))}
              </select>
            </Field>
            <Button disabled={!canWrite || !userId} onClick={() => void grant()}>
              Grant role
            </Button>
          </div>
          <div className="mt-3">
            <Message>{message}</Message>
          </div>
        </CardBody>
      </Card>
      <Card>
        <CardHeader title="Active staff" />
        <CardBody>
          <div className="space-y-2">
            {grants.map((grant) => (
              <div
                key={grant.id}
                className="flex items-center justify-between gap-3 rounded-lg border border-[var(--color-border-subtle)] p-3"
              >
                <div>
                  <p className="text-sm font-medium">{grant.email}</p>
                  <Badge tone="accent">{grant.role}</Badge>
                </div>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={!canWrite}
                  onClick={() => void revoke(grant.id)}
                >
                  Revoke
                </Button>
              </div>
            ))}
          </div>
        </CardBody>
      </Card>
    </div>
  );
}

const TASKS = [
  "mock_interviewer_turn",
  "rubric_evaluate_answer",
  "session_report",
  "copilot_answer",
  "answer_direction",
  "resume_extract",
  "jd_extract",
  "story_generate",
  "question_classify",
];

export function PromptsPane({ canWrite }: { canWrite: boolean }) {
  const [rows, setRows] = useState<PromptVersion[] | null>(null);
  const [source, setSource] = useState<PromptVersion | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const load = useCallback(() => listPrompts().then(setRows), []);
  useEffect(() => {
    void load();
  }, [load]);
  async function create() {
    if (!source) return;
    try {
      await createPrompt({
        prompt_id: source.prompt_id,
        task_class: source.task_class,
        system_template: source.system_template,
        user_template: source.user_template,
        variables: source.variables,
        output_schema: source.output_schema,
        notes: source.notes,
      });
      setMessage("Development version created. Review it before activation.");
      setSource(null);
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not create version");
    }
  }
  async function activate(row: PromptVersion) {
    try {
      await activatePrompt(row.prompt_id, row.version);
      setMessage(`${row.prompt_id}@${row.version} is now production.`);
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not activate prompt");
    }
  }
  if (!rows) return <Loading rows={5} />;
  return (
    <div className="space-y-4">
      <Message>{message}</Message>
      {source ? (
        <Card>
          <CardHeader
            title={`New ${source.prompt_id} version`}
            meta="Creating a version does not affect production until it is activated."
            action={
              <Button variant="ghost" size="sm" onClick={() => setSource(null)}>
                Cancel
              </Button>
            }
          />
          <CardBody>
            <div className="space-y-4">
              <div className="grid gap-3 md:grid-cols-2">
                <TextField
                  label="Prompt ID"
                  value={source.prompt_id}
                  disabled={!canWrite}
                  onChange={(x) => setSource({ ...source, prompt_id: x })}
                />
                <Field label="Task class">
                  <select
                    className="field"
                    value={source.task_class}
                    onChange={(event) =>
                      setSource({ ...source, task_class: event.target.value })
                    }
                  >
                    {TASKS.map((task) => (
                      <option key={task}>{task}</option>
                    ))}
                  </select>
                </Field>
              </div>
              <Field label="System instruction">
                <textarea
                  className="field min-h-36"
                  value={source.system_template}
                  onChange={(event) =>
                    setSource({ ...source, system_template: event.target.value })
                  }
                />
              </Field>
              <Field label="User template">
                <textarea
                  className="field min-h-56 font-mono text-xs"
                  value={source.user_template}
                  onChange={(event) =>
                    setSource({ ...source, user_template: event.target.value })
                  }
                />
              </Field>
              <TextField
                label="Variables (comma separated)"
                value={source.variables.join(", ")}
                disabled={!canWrite}
                onChange={(x) =>
                  setSource({
                    ...source,
                    variables: x
                      .split(",")
                      .map((item) => item.trim())
                      .filter(Boolean),
                  })
                }
              />
              <Field label="Output JSON schema">
                <textarea
                  className="field min-h-44 font-mono text-xs"
                  value={JSON.stringify(source.output_schema, null, 2)}
                  onChange={(event) => {
                    try {
                      setSource({
                        ...source,
                        output_schema: JSON.parse(event.target.value) as Record<
                          string,
                          unknown
                        >,
                      });
                    } catch {
                      /* keep the last valid schema while typing */
                    }
                  }}
                />
              </Field>
              <Button onClick={() => void create()} disabled={!canWrite}>
                Create development version
              </Button>
            </div>
          </CardBody>
        </Card>
      ) : null}
      <Card>
        <CardHeader
          title="Prompt registry"
          meta="Versioned prompts support explicit promotion and rollback. Built-in versions remain visible as the baseline."
        />
        <CardBody>
          <div className="space-y-3">
            {rows.map((row) => (
              <div
                key={`${row.prompt_id}-${row.version}`}
                className="rounded-xl border border-[var(--color-border-subtle)] p-4"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div className="flex items-center gap-2">
                      <code>
                        {row.prompt_id}@{row.version}
                      </code>
                      <Badge tone={row.status === "production" ? "positive" : "neutral"}>
                        {row.status}
                      </Badge>
                    </div>
                    <p className="mt-2 text-sm text-[var(--color-text-secondary)]">
                      {row.notes || row.task_class}
                    </p>
                  </div>
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      variant="secondary"
                      disabled={!canWrite}
                      onClick={() => setSource({ ...row })}
                    >
                      New version
                    </Button>
                    {row.id && row.status !== "production" ? (
                      <Button size="sm" disabled={!canWrite} onClick={() => void activate(row)}>
                        Activate
                      </Button>
                    ) : null}
                  </div>
                </div>
                <details className="mt-3">
                  <summary className="cursor-pointer text-sm text-[var(--color-accent)]">
                    Inspect templates
                  </summary>
                  <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap rounded-lg bg-[var(--color-raised)] p-3 text-xs">
                    {row.system_template}
                    {"\n\n"}
                    {row.user_template}
                  </pre>
                </details>
              </div>
            ))}
          </div>
        </CardBody>
      </Card>
    </div>
  );
}
