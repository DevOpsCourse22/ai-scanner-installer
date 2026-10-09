import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type Answers, type LogLine, type RunState, type Status } from "./api";
import Logo from "./Logo";
import LogoSweep from "./LogoSweep";

type StepId = "unlock" | "where" | "repos" | "model" | "people" | "review" | "deploy";
const STEPS: { id: StepId; label: string; bar: number }[] = [
  { id: "unlock", label: "Saved answers", bar: 46 },
  { id: "where", label: "Where it runs", bar: 70 },
  { id: "repos", label: "Repositories", bar: 58 },
  { id: "model", label: "AI model", bar: 64 },
  { id: "people", label: "Admins", bar: 40 },
  { id: "review", label: "Review", bar: 52 },
  { id: "deploy", label: "Deploy", bar: 76 },
];

const PRESETS = [
  { id: "openai", label: "OpenAI", model: "gpt-4.1-mini" },
  { id: "anthropic", label: "Anthropic", model: "claude-sonnet-5-5" },
  { id: "google", label: "Google Gemini", model: "gemini-2.5-flash" },
  { id: "groq", label: "Groq", model: "llama-3.3-70b-versatile" },
  { id: "openrouter", label: "OpenRouter", model: "qwen/qwen-2.5-coder-32b-instruct" },
  { id: "mistral", label: "Mistral", model: "codestral-latest" },
];

const REGIONS = ["us-east-1", "us-east-2", "us-west-2", "eu-west-1", "eu-central-1", "il-central-1"];

export default function App() {
  const [status, setStatus] = useState<Status | null>(null);
  const [answers, setAnswers] = useState<Answers>({});
  const [step, setStep] = useState<StepId>("unlock");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [maxVisited, setMaxVisited] = useState(0);

  useEffect(() => {
    api.status().then(async (s) => {
      setStatus(s);
      if (!s.unlocked) return;
      // the page was reloaded: the server still holds the answers in memory
      setAnswers((await api.getAnswers()).answers);
      setSaved(true);
      setMaxVisited(STEPS.length - 2);
      setStep(s.run ? "deploy" : "review");
    }).catch((e) => setError(e.message));
  }, []);

  // keyboard and screen-reader users land on the new step's heading
  useEffect(() => {
    const h = document.querySelector<HTMLElement>(".panel h1");
    if (h && step !== "unlock") { h.tabIndex = -1; h.focus({ preventScroll: true }); }
  }, [step]);

  const patch = useCallback((fn: (a: Answers) => void) => {
    setAnswers((prev) => {
      const next = structuredClone(prev);
      fn(next);
      return next;
    });
  }, []);

  async function go(to: StepId) {
    setError("");
    if (step !== "unlock" && step !== "deploy" && Object.keys(answers).length) {
      setSaving(true);
      try {
        setAnswers((await api.save(answers)).answers);
      } catch (e) {
        setError((e as Error).message);
        setSaving(false);
        return;
      }
      setSaving(false);
      setSaved(true);
    }
    setStep(to);
    setMaxVisited((m) => Math.max(m, STEPS.findIndex((s) => s.id === to)));
    window.scrollTo({ top: 0 });
  }

  const index = STEPS.findIndex((s) => s.id === step);
  const next = STEPS[index + 1]?.id;
  const back = STEPS[index - 1]?.id;
  const missing = stepMissing(step, answers);

  return (
    <div className="shell">
      <header className="top">
        <Logo size={30} />
        <span className="top-name">AI Scan Repos installer</span>
        {saved ? (
          <span className="top-saved" title="Answers are saved encrypted on the installer's volume">
            {saving ? "Saving" : "Answers saved, encrypted"}
          </span>
        ) : null}
      </header>

      <nav className="rail" aria-label="Steps">
        <ol>
          {STEPS.map((s, i) => {
            const state = i < index ? "done" : i === index ? "current" : "todo";
            const reachable = step !== "unlock" && step !== "deploy" && s.id !== "unlock" && s.id !== "deploy"
              && i <= maxVisited && (i <= index || missing.length === 0);
            return (
              <li key={s.id} className={`rail-item ${state}`}>
                <button type="button" disabled={!reachable} onClick={() => go(s.id)}
                        aria-current={state === "current" ? "step" : undefined}>
                  <span className="rail-bar" style={{ width: s.bar }}>
                    {state === "current" ? <span className="rail-mark" /> : null}
                  </span>
                  <span className="rail-label">{s.label}</span>
                  <span className="sr-only">{state === "done" ? " (done)" : ""}</span>
                </button>
              </li>
            );
          })}
        </ol>
      </nav>

      <main className="panel">
        <p className="step-count">Step {index + 1} of {STEPS.length}: {STEPS[index].label}</p>
        {error ? <p className="alert" role="alert">{error}</p> : null}
        {status === null && !error ? <p className="muted">Starting the installer</p> : null}
        {status && step === "unlock" ? (
          <Unlock status={status} onOpen={(a) => { setAnswers(a); setSaved(true); setMaxVisited(1); setStep("where"); }} />
        ) : null}
        {status && step === "where" ? <Where status={status} a={answers} patch={patch} /> : null}
        {step === "repos" ? <Repos a={answers} patch={patch} /> : null}
        {step === "model" ? <Model a={answers} patch={patch} /> : null}
        {step === "people" ? <People a={answers} patch={patch} /> : null}
        {step === "review" ? <Review a={answers} onRun={() => setStep("deploy")} /> : null}
        {step === "deploy" ? <Deploy onBack={() => setStep("review")} /> : null}

        {step !== "unlock" && step !== "deploy" ? (
          <>
            {missing.length && step !== "review" ? (
              <p className="hint">Still needed: {missing.join(", ")}.</p>
            ) : null}
            <div className={`nav ${step === "review" ? "nav-review" : ""}`}>
              {back && back !== "unlock" ? (
                <button type="button" className="btn ghost" onClick={() => go(back)}>Back</button>
              ) : <span />}
              {next && step !== "review" ? (
                <button type="button" className="btn primary" disabled={saving || missing.length > 0}
                        onClick={() => go(next)}>
                  Continue
                </button>
              ) : null}
            </div>
          </>
        ) : null}
      </main>
    </div>
  );
}

/** What a step still needs before Continue (the server checks again on Deploy). */
function stepMissing(step: StepId, a: Answers): string[] {
  const t = a.target ?? {}, ai = a.ai ?? {}, m: string[] = [];
  const port = (v: unknown) => v === undefined || (Number.isInteger(Number(v)) && Number(v) >= 1 && Number(v) <= 65535);
  if (step === "where") {
    if (!t.kind) m.push("where it runs");
    if (t.kind === "local" && !t.context) m.push("a cluster");
    if (t.kind === "ssh") {
      if (!t.ssh?.host) m.push("server address");
      if (!t.ssh?.user) m.push("SSH user");
      if (!t.ssh?.private_key && !t.ssh?.private_key_saved) m.push("SSH key");
      if (!port(t.ssh?.port)) m.push("an SSH port from 1 to 65535");
    }
    if (t.kind === "eks") {
      if (!t.eks?.region) m.push("a region");
      if (!t.eks?.aws_profile && !(t.eks?.aws_access_key_id && (t.eks?.aws_secret_access_key || t.eks?.aws_secret_access_key_saved)))
        m.push("AWS credentials");
      const n = Number(t.eks?.node_count ?? 2);
      if (!(Number.isInteger(n) && n >= 1 && n <= 10)) m.push("1 to 10 nodes");
    }
  }
  if (step === "repos") {
    if (!a.repos?.owner) m.push("GitHub owner");
    if (!a.repos?.token && !a.repos?.token_saved) m.push("GitHub token");
  }
  if (step === "model") {
    if (!ai.mode) m.push("a choice");
    if (ai.mode === "same_machine" && !ai.endpoint) m.push("the model server's address");
    if (ai.mode === "tailnet") {
      if (!ai.tailnet_ip) m.push("tailnet IP");
      if (!ai.ts_client_id) m.push("Tailscale client ID");
      if (!ai.ts_client_secret && !ai.ts_client_secret_saved) m.push("Tailscale client secret");
      if (!port(ai.tailnet_port)) m.push("a port from 1 to 65535");
    }
    if (ai.mode === "cloud") {
      if (!ai.preset) m.push("a provider");
      if (!ai.api_key && !ai.api_key_saved) m.push("API key");
    }
  }
  if (step === "people" && !(a.admins ?? []).length) m.push("at least one admin");
  return m;
}

/* ---------- small pieces ---------- */

function Field(props: {
  label: string; value?: string | number; onChange: (v: string) => void; hint?: string;
  secret?: boolean; saved?: boolean; placeholder?: string; type?: string; multiline?: boolean; id?: string;
  autoFocus?: boolean;
}) {
  const id = props.id ?? props.label.toLowerCase().replace(/[^a-z0-9]+/g, "-");
  const placeholder = props.saved ? "Saved. Leave empty to keep it." : props.placeholder;
  return (
    <div className="field">
      <label htmlFor={id}>{props.label}</label>
      {props.multiline ? (
        <textarea id={id} value={props.value ?? ""} placeholder={placeholder} rows={5} spellCheck={false}
                  onChange={(e) => props.onChange(e.target.value)} />
      ) : (
        <input id={id} type={props.secret ? "password" : props.type ?? "text"} value={props.value ?? ""}
               placeholder={placeholder} autoComplete="off" spellCheck={false} autoFocus={props.autoFocus}
               onChange={(e) => props.onChange(e.target.value)} />
      )}
      {props.hint ? <p className="hint">{props.hint}</p> : null}
    </div>
  );
}

function Choice<T extends string>(props: {
  name: string; label: string; value?: T; options: { id: T; title: string; text: string }[]; onChange: (v: T) => void;
}) {
  return (
    <div className="choices" role="radiogroup" aria-label={props.label}>
      {props.options.map((o) => (
        <label key={o.id} className={`choice ${props.value === o.id ? "on" : ""}`}>
          <input type="radio" name={props.name} checked={props.value === o.id} onChange={() => props.onChange(o.id)} />
          <span className="choice-title">{o.title}</span>
          <span className="choice-text">{o.text}</span>
        </label>
      ))}
    </div>
  );
}

function Copy({ text, label = "" }: { text: string; label?: string }) {
  const [done, setDone] = useState("");
  return (
    <button type="button" className="copy" aria-label={label ? `Copy ${label}` : "Copy"} onClick={() => {
      navigator.clipboard.writeText(text).then(() => setDone("Copied"), () => setDone("Select and copy it"))
        .finally(() => setTimeout(() => setDone(""), 1600));
    }}><span role="status">{done || "Copy"}</span></button>
  );
}

/* ---------- steps ---------- */

function Unlock({ status, onOpen }: { status: Status; onOpen: (a: Answers) => void }) {
  const [pass, setPass] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const fresh = !status.vault_exists;

  async function open(isNew: boolean) {
    if (isNew && !fresh && !window.confirm("Start over with empty answers? The generated passwords and the GitHub OAuth App are kept.")) return;
    setBusy(true);
    setErr("");
    try {
      onOpen((await api.unlock(pass, isNew)).answers);
    } catch (e) {
      setErr((e as Error).message);
    }
    setBusy(false);
  }

  return (
    <form className="step" onSubmit={(e) => { e.preventDefault(); open(fresh); }}>
      <h1>{fresh ? "Deploy AI Scan Repos" : "Welcome back"}</h1>
      <p className="lead">
        {fresh
          ? "A few answers, two things to do on GitHub, then the installer builds and deploys everything."
          : "Your answers from last time are saved. Open them to upgrade or remove the install."}
      </p>
      <Field label={fresh ? "Choose a passphrase" : "Passphrase"} secret value={pass} onChange={setPass} autoFocus
             hint={fresh ? "It encrypts your answers on the installer's volume, so a later run can upgrade or "
                         + "remove this install. At least 8 characters." : "At least 8 characters."} />
      {err ? <p className="alert" role="alert">{err}</p> : null}
      <div className="nav">
        {fresh ? <span /> : (
          <button type="button" className="btn ghost" disabled={busy || pass.length < 8} onClick={() => open(true)}>
            Start over
          </button>
        )}
        <button type="submit" className="btn primary" disabled={busy || pass.length < 8}>
          {fresh ? "Continue" : "Open answers"}
        </button>
      </div>
    </form>
  );
}

type StepProps = { a: Answers; patch: (fn: (a: Answers) => void) => void };

function Where({ status, a, patch }: StepProps & { status: Status }) {
  const t = a.target ?? {};
  const kind = t.kind;
  const ctxs = status.contexts;
  return (
    <section className="step">
      <h1>Where should it run?</h1>
      <Choice name="kind" label="Where it runs" value={kind} onChange={(v) => patch((x) => { x.target = { ...(x.target ?? {}), kind: v }; })}
        options={[
          { id: "local", title: "A cluster on this computer", text: "minikube, Docker Desktop or any context in your kubeconfig." },
          { id: "ssh", title: "A server I can SSH into", text: "K3s or kubeadm on a machine on your network." },
          { id: "eks", title: "Amazon EKS", text: "Terraform creates the VPC, cluster, storage and image repositories." },
        ]} />

      {kind === "local" ? (
        ctxs.length ? (
          <div className="field">
            <label htmlFor="context">Cluster</label>
            <select id="context" value={t.context ?? ""} onChange={(e) => patch((x) => { x.target!.context = e.target.value; })}>
              <option value="">Choose a context</option>
              {ctxs.map((c) => <option key={c.name} value={c.name}>{c.name}{c.current ? " (current)" : ""}</option>)}
            </select>
            {t.context === "minikube" && !status.minikube_mounted ? (
              <p className="hint warn">Restart the installer with your .minikube folder mounted; the README's docker run line shows it.</p>
            ) : null}
          </div>
        ) : (
          <p className="alert">No kubeconfig is mounted. Restart the installer with the docker run line from the README.</p>
        )
      ) : null}

      {kind === "ssh" ? (
        <div className="grid2">
          <Field label="Server address" value={t.ssh?.host} placeholder="192.168.1.20"
                 onChange={(v) => patch((x) => { x.target!.ssh = { ...(x.target!.ssh ?? {}), host: v.trim() }; })} />
          <Field label="SSH user" value={t.ssh?.user} placeholder="ubuntu"
                 onChange={(v) => patch((x) => { x.target!.ssh = { ...(x.target!.ssh ?? {}), user: v.trim() }; })} />
          <div className="span2">
            <Field label="SSH private key" multiline secret saved={t.ssh?.private_key_saved} value={t.ssh?.private_key}
                   placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                   hint="Paste the key's text. It stays in memory and in the encrypted answers."
                   onChange={(v) => patch((x) => { x.target!.ssh = { ...(x.target!.ssh ?? {}), private_key: v }; })} />
          </div>
          <Field label="sudo password (if sudo asks for one)" secret saved={t.ssh?.sudo_password_saved}
                 value={t.ssh?.sudo_password}
                 onChange={(v) => patch((x) => { x.target!.ssh = { ...(x.target!.ssh ?? {}), sudo_password: v }; })} />
          <Field label="SSH port" value={t.ssh?.port} placeholder="22" type="number"
                 onChange={(v) => patch((x) => { x.target!.ssh = { ...(x.target!.ssh ?? {}), port: v === "" ? undefined : Number(v) }; })} />
        </div>
      ) : null}

      {kind === "eks" ? <EksFields status={status} a={a} patch={patch} /> : null}

      {kind ? (
        <Field label="App address (optional)" value={a.public_url} placeholder="Found automatically"
               hint="Only if people will reach the app on another address, like a DNS name you point at it."
               onChange={(v) => patch((x) => { x.public_url = v.trim() || undefined; })} />
      ) : null}
    </section>
  );
}

function EksFields({ status, a, patch }: StepProps & { status: Status }) {
  const e = a.target?.eks ?? {};
  const set = (k: string, v: unknown) => patch((x) => {
    x.target!.eks = { ...(x.target!.eks ?? {}), [k]: v };
  });
  const useKeys = !e.aws_profile;
  return (
    <div className="grid2">
      <div className="field">
        <label htmlFor="region">Region</label>
        <select id="region" value={e.region ?? ""} onChange={(ev) => set("region", ev.target.value)}>
          <option value="">Choose a region</option>
          {REGIONS.map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
      </div>
      <Field label="Name" value={e.name ?? "ai-scan-repos"} onChange={(v) => set("name", v.trim())} />
      <div className="field span2">
        <label htmlFor="creds">AWS credentials</label>
        <select id="creds" value={e.aws_profile ?? ""} onChange={(ev) => set("aws_profile", ev.target.value || undefined)}>
          <option value="">Access keys (type them below)</option>
          {status.aws_profiles.map((p) => <option key={p} value={p}>Profile: {p}</option>)}
        </select>
        {status.aws_profiles.length === 0 ? (
          <p className="hint">Mount ~/.aws to pick a profile instead (README).</p>
        ) : null}
      </div>
      {useKeys ? (
        <>
          <Field label="Access key ID" value={e.aws_access_key_id} onChange={(v) => set("aws_access_key_id", v.trim())} />
          <Field label="Secret access key" secret saved={e.aws_secret_access_key_saved} value={e.aws_secret_access_key}
                 onChange={(v) => set("aws_secret_access_key", v.trim())} />
        </>
      ) : null}
      <Field label="Node type" value={e.node_type ?? "t3.large"} onChange={(v) => set("node_type", v.trim())} />
      <Field label="Nodes" type="number" value={e.node_count} placeholder="2" onChange={(v) => set("node_count", v === "" ? undefined : Number(v))} />
      <div className="span2">
        <Field label="Domain for HTTPS (optional)" value={e.domain} placeholder="scan.example.com"
               hint="Leave empty to use the load balancer's address over http. With a domain you add two DNS records during the deploy."
               onChange={(v) => set("domain", v.trim())} />
      </div>
    </div>
  );
}

function Repos({ a, patch }: StepProps) {
  const r = a.repos ?? {};
  const [check, setCheck] = useState<Awaited<ReturnType<typeof api.checkGithub>> | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  async function run() {
    setBusy(true); setErr("");
    try {
      const res = await api.checkGithub(r.owner ?? "", r.token ?? "");
      setCheck(res);
      if (res.login) patch((x) => { x.repos = { ...(x.repos ?? {}), login: res.login }; });
    } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  }
  return (
    <section className="step">
      <h1>Which repositories?</h1>
      <div className="grid2">
        <Field label="GitHub owner" value={r.owner} hint="DevOpsCourse22, or your own account if you forked the six repositories."
               onChange={(v) => patch((x) => { x.repos = { ...(x.repos ?? {}), owner: v.trim() }; })} />
        <Field label="Installer repository" value={r.installer_repo} placeholder="DevOpsCourse22/ai-scanner-installer"
               hint="This install's manifests and pipelines go to a branch here. The six project repositories are only read."
               onChange={(v) => patch((x) => { x.repos = { ...(x.repos ?? {}), installer_repo: v.trim() || undefined }; })} />
        <Field label="GitHub token" secret saved={r.token_saved} value={r.token}
               hint="Classic token with repo and admin:repo_hook, or fine-grained: Contents read (project repos), Contents write (installer repo), Webhooks write (code repos)."
               onChange={(v) => patch((x) => { x.repos = { ...(x.repos ?? {}), token: v.trim() }; })} />
      </div>
      <p className="row">
        <a className="link" href="https://github.com/settings/tokens/new?scopes=repo,admin:repo_hook&description=ai-scan-repos-installer"
           target="_blank" rel="noreferrer">Create a token on GitHub</a>
        <button type="button" className="btn ghost" disabled={busy || !r.owner || (!r.token && !r.token_saved)} onClick={run}>
          {busy ? "Checking" : "Check access"}
        </button>
      </p>
      {err ? <p className="alert">{err}</p> : null}
      {check ? (
        <div className="checks">
          {check.error ? <p className="alert">{check.error}</p> : null}
          {check.login ? <p className="muted">Signed in as {check.login}</p> : null}
          <ul className="checklist">
            {check.repos?.map((x) => {
              const kind = x.name.includes("/") ? "installer" : x.name.endsWith("-deployment") ? "read" : "code";
              const ok = x.found && (kind === "installer" ? x.push : kind === "code" ? x.admin : true);
              const say = !x.found ? "not found" : kind === "read" ? "can read"
                : kind === "installer" ? (ok ? "can push" : "cannot push") : (ok ? "can add webhooks" : "cannot add webhooks");
              return (
                <li key={x.name} className={ok ? "ok" : "bad"}>
                  <span className="mono">{x.name}</span>
                  <span>{say}</span>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

function Model({ a, patch }: StepProps) {
  const ai = a.ai ?? {};
  const set = (k: string, v: unknown) => patch((x) => { x.ai = { ...(x.ai ?? {}), [k]: v }; });
  const [probe, setProbe] = useState<{ ok: boolean; models?: string[]; error?: string } | null>(null);
  const [probing, setProbing] = useState(false);
  const kind = a.target?.kind;
  const suggested = kind === "local" ? "http://host.minikube.internal:1234/v1" : "http://192.168.1.10:1234/v1";
  return (
    <section className="step">
      <h1>Which AI model reviews code by default?</h1>
      <p className="lead">Admins can change it later in the app's Settings.</p>
      <Choice name="ai" label="Default AI model" value={ai.mode} onChange={(v) => set("mode", v)} options={[
        { id: "same_machine", title: "A model server on my network", text: "LM Studio, Ollama or vLLM that the cluster can reach directly." },
        { id: "tailnet", title: "A model server on my tailnet", text: "Reached through Tailscale, like K3S-SRV reaches LM Studio today." },
        { id: "cloud", title: "A cloud provider", text: "OpenAI, Anthropic, Gemini, Groq, OpenRouter or Mistral, with an API key." },
      ]} />

      {ai.mode === "same_machine" ? (
        <>
          <Field label="Model server address" value={ai.endpoint} placeholder={suggested}
                 hint={kind === "local" ? "From inside minikube, your computer is host.minikube.internal (Docker Desktop: host.docker.internal). LM Studio must serve on the local network."
                                        : "An address the cluster's nodes can reach, ending in /v1."}
                 onChange={(v) => { setProbe(null); set("endpoint", v.trim()); }} />
          <p className="row">
            <button type="button" className="btn ghost" disabled={!ai.endpoint || probing} onClick={async () => {
              setProbing(true);
              setProbe(await api.checkModel(ai.endpoint!).catch((e) => ({ ok: false, error: (e as Error).message })));
              setProbing(false);
            }}>{probing ? "Testing" : "Test connection"}</button>
            {probe ? <span className={probe.ok ? "okText" : "badText"}>
              {probe.ok ? `Reachable: ${probe.models?.slice(0, 3).join(", ") || "no models loaded"}` : "Not reachable from the installer. The cluster may still reach it."}
            </span> : null}
          </p>
          <Field label="Model (optional)" value={ai.model} placeholder="The server's loaded model" onChange={(v) => set("model", v.trim())} />
        </>
      ) : null}

      {ai.mode === "tailnet" ? (
        <div className="grid2">
          <Field label="Model host's tailnet IP" value={ai.tailnet_ip} placeholder="100.x.y.z" onChange={(v) => set("tailnet_ip", v.trim())} />
          <Field label="Port" type="number" value={ai.tailnet_port} placeholder="1234" onChange={(v) => set("tailnet_port", v === "" ? undefined : Number(v))} />
          <Field label="Tailscale OAuth client ID" value={ai.ts_client_id} onChange={(v) => set("ts_client_id", v.trim())} />
          <Field label="Tailscale OAuth client secret" secret saved={ai.ts_client_secret_saved} value={ai.ts_client_secret}
                 onChange={(v) => set("ts_client_secret", v.trim())} />
          <p className="hint span2">
            <a className="link" href="https://login.tailscale.com/admin/settings/trust-credentials" target="_blank" rel="noreferrer">New OAuth client</a>:
            write on General/Services, Devices/Core, Keys/Auth Keys; tag <span className="mono">tag:k8s-operator</span>.
          </p>
          <Field label="Model (optional)" value={ai.model} onChange={(v) => set("model", v.trim())} />
        </div>
      ) : null}

      {ai.mode === "cloud" ? (
        <div className="grid2">
          <div className="field">
            <label htmlFor="preset">Provider</label>
            <select id="preset" value={ai.preset ?? ""} onChange={(e) => {
              const p = PRESETS.find((x) => x.id === e.target.value);
              patch((x) => { x.ai = { ...(x.ai ?? {}), preset: e.target.value, model: p?.model ?? "" }; });
            }}>
              <option value="">Choose a provider</option>
              {PRESETS.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
            </select>
          </div>
          <Field label="Model" value={ai.model} onChange={(v) => set("model", v.trim())} />
          <div className="span2">
            <Field label="API key" secret saved={ai.api_key_saved} value={ai.api_key}
                   hint="Stored in the cluster as a Secret and encrypted in the app's database." onChange={(v) => set("api_key", v.trim())} />
          </div>
        </div>
      ) : null}
    </section>
  );
}

function People({ a, patch }: StepProps) {
  const [draft, setDraft] = useState("");
  const admins = a.admins ?? [];
  const login = a.repos?.login;
  function add(name: string) {
    const n = name.trim().replace(/^@/, "");
    if (n && !admins.includes(n)) patch((x) => { x.admins = [...(x.admins ?? []), n]; });
    setDraft("");
  }
  return (
    <section className="step">
      <h1>Who are the admins?</h1>
      <p className="lead">GitHub usernames that may change the app's settings, like the AI model. Anyone else can sign in and run reviews.</p>
      <form className="row" onSubmit={(e) => { e.preventDefault(); add(draft); }}>
        <input aria-label="GitHub username" value={draft} placeholder="GitHub username" onChange={(e) => setDraft(e.target.value)} />
        <button type="submit" className="btn ghost" disabled={!draft.trim()}>Add</button>
        {login && !admins.includes(login) ? (
          <button type="button" className="btn ghost" onClick={() => add(login)}>Add {login}</button>
        ) : null}
      </form>
      <ul className="tags">
        {admins.map((n) => (
          <li key={n}>
            <span className="mono">{n}</span>
            <button type="button" aria-label={`Remove ${n}`} onClick={() => patch((x) => { x.admins = admins.filter((y) => y !== n); })}>Remove</button>
          </li>
        ))}
      </ul>
      {admins.length === 0 ? <p className="muted">Add at least one admin.</p> : null}
    </section>
  );
}

function Review({ a, onRun }: { a: Answers; onRun: () => void }) {
  const [found, setFound] = useState<{ installed: boolean; error?: string } | null>(null);
  const [err, setErr] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [missing, setMissing] = useState<string[]>([]);
  useEffect(() => {
    api.checkAnswers().then((r) => {
      setMissing(r.missing);
      if (r.missing.length) { setFound({ installed: false }); return; }
      api.checkTarget().then(setFound).catch((e) => setFound({ installed: false, error: (e as Error).message }));
    }).catch((e) => { setErr((e as Error).message); setFound({ installed: false }); });
  }, []);
  const v = (x: unknown) => (x === undefined || x === null || x === "" ? "Not set" : String(x));

  const t = a.target ?? {};
  const where = t.kind === "eks" ? `Amazon EKS in ${v(t.eks?.region)} (${t.eks?.node_count ?? 2} × ${t.eks?.node_type ?? "t3.large"})`
    : t.kind === "ssh" ? `${v(t.ssh?.user)}@${v(t.ssh?.host)} over SSH` : t.kind === "local" ? `kube context ${v(t.context)}` : "Not set";
  const ai = a.ai ?? {};
  const model = ai.mode === "cloud" ? `${PRESETS.find((p) => p.id === ai.preset)?.label ?? ai.preset}, ${ai.model || "default model"}`
    : ai.mode === "tailnet" ? `Tailnet host ${v(ai.tailnet_ip)}:${ai.tailnet_port ?? 1234}`
    : ai.mode === "same_machine" ? `Model server at ${v(ai.endpoint)}` : "Not set";
  const rows: [string, string][] = [
    ["Where", where],
    ["Repositories", v(a.repos?.owner)],
    ["Default model", model],
    ["Admins", (a.admins ?? []).join(", ") || "Not set"],
    ["Address", a.public_url || "found during the deploy"],
  ];

  async function start(kind: string) {
    setBusy(true); setErr("");
    try { await api.run(kind, confirm); onRun(); } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  }

  return (
    <section className="step">
      <h1>Ready to deploy</h1>
      <dl className="summary">
        {rows.map(([k, v]) => (<div key={k}><dt>{k}</dt><dd>{v}</dd></div>))}
      </dl>
      <p className="lead">During the deploy you create one GitHub OAuth App; the installer shows the exact addresses when it knows them.
        {t.kind === "eks" ? " EKS takes about 20 minutes to create and costs money while it runs." : ""}</p>
      {missing.length ? (
        <div className="alert" role="alert"><p>Fill these in first:</p><ul>{missing.map((m) => <li key={m}>{m}</li>)}</ul></div>
      ) : null}
      {found?.error ? <p className="hint warn">Could not look at the cluster yet: {found.error}</p> : null}
      {err ? <p className="alert" role="alert">{err}</p> : null}
      <div className="nav">
        <span />
        {found?.installed ? (
          <button type="button" className="btn primary" disabled={busy} onClick={() => start("upgrade")}>Upgrade</button>
        ) : (
          <button type="button" className="btn primary" disabled={busy || found === null || missing.length > 0} onClick={() => start("install")}>
            {found === null ? "Looking at the cluster" : "Deploy"}
          </button>
        )}
      </div>
      {found?.installed ? (
        <div className="danger-zone">
          <h2>Remove</h2>
          <p>Deletes the app, Jenkins, Argo CD and the webhooks{t.kind === "eks" ? ", then every AWS resource Terraform created" : ""}. The repositories keep their code. Type <span className="mono">destroy</span> to confirm.</p>
          <div className="row">
            <input aria-label="Type destroy to confirm" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
            <button type="button" className="btn danger" disabled={busy || confirm !== "destroy"} onClick={() => start("destroy")}>Remove</button>
          </div>
        </div>
      ) : null}
    </section>
  );
}

function Deploy({ onBack }: { onBack: () => void }) {
  const [run, setRun] = useState<RunState | null>(null);
  const [lines, setLines] = useState<LogLine[]>([]);
  const [showAll, setShowAll] = useState(false);
  const logRef = useRef<HTMLPreElement>(null);
  const [lost, setLost] = useState("");
  const [announce, setAnnounce] = useState("");

  useEffect(() => {
    let es: EventSource | null = null, count = 0, ended = false, timer = 0;
    const connect = () => {
      es = new EventSource(`/api/run/events?start=${count}`);
      es.addEventListener("line", (e) => {
        const line: LogLine = JSON.parse((e as MessageEvent).data);
        if (line.i < count) return;          // already shown before a reconnect
        count = line.i + 1;
        setLines((prev) => [...prev, line]);
      });
      es.addEventListener("state", (e) => { setLost(""); setRun(JSON.parse((e as MessageEvent).data)); });
      es.addEventListener("end", () => { ended = true; es?.close(); });
      es.onerror = () => {
        es?.close();
        if (ended) return;
        api.getRun().then((r) => {
          if (!("status" in r)) { setLost("No deploy is running."); return; }
          timer = window.setTimeout(connect, 2000);
        }).catch(() => { setLost("Lost the connection to the installer. Retrying."); timer = window.setTimeout(connect, 3000); });
      };
    };
    connect();
    return () => { ended = true; es?.close(); window.clearTimeout(timer); };
  }, []);

  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines]);

  const current = run?.steps.find((s) => ["running", "waiting"].includes(s.status));
  const failed = run?.steps.find((s) => s.status === "failed");
  useEffect(() => {
    if (failed) setAnnounce(`${failed.title} failed`);
    else if (run?.action) setAnnounce("Your turn: " + run.action.message);
    else if (current) setAnnounce(current.title);
  }, [current?.id, failed?.id, run?.action?.message]); // eslint-disable-line react-hooks/exhaustive-deps
  const visible = useMemo(() => showAll ? lines : lines.filter((l) => !current || l.step === current.id || l.level === "error"),
    [lines, showAll, current]);
  const title = run?.kind === "destroy" ? "Removing" : run?.kind === "upgrade" ? "Upgrading" : "Deploying";

  if (run?.status === "done" && run.kind !== "destroy") return <Done run={run} />;

  return (
    <section className="step">
      <h1>{run?.status === "done" ? "Removed" : run?.status === "failed" ? `${title} stopped` : title}</h1>
      <p className="sr-only" role="status">{announce}</p>
      {lost ? (
        <div className="alert" role="alert"><p>{lost}</p>
          <button type="button" className="btn ghost" onClick={onBack}>Back to review</button></div>
      ) : null}
      <ol className="progress">
        {run?.steps.map((s) => (
          <li key={s.id} className={`p-${s.status}`} aria-current={["running", "waiting"].includes(s.status) ? "step" : undefined}>
            <span className="p-bar">{["running", "waiting"].includes(s.status) ? <span className="rail-mark" /> : null}</span>
            <span className="p-title">{s.title}<span className="sr-only">{
              { done: ", done", running: ", running", waiting: ", waiting for you", failed: ", failed" }[s.status] ?? ""}</span></span>
            {s.status === "failed" ? <span className="p-detail">{s.detail}</span> : null}
          </li>
        ))}
      </ol>

      {run?.action ? <ActionCard key={run.action.step + run.action.message} action={run.action} /> : null}

      {failed ? (
        <div className="alert" role="alert">
          <p><strong>{failed.title}</strong> failed. {failed.detail}</p>
          <p>Fix it, then go back and run again: finished steps are safe to repeat.</p>
          <button type="button" className="btn ghost" onClick={onBack}>Back to review</button>
        </div>
      ) : null}

      {run && ["running", "waiting"].includes(run.status) ? (
        <p className="row">
          <button type="button" className="link" onClick={() => {
            if (window.confirm("Stop after the current step? Finished steps stay done.")) api.cancelRun().catch(() => undefined);
          }}>Cancel</button>
        </p>
      ) : null}
      <div className="log-head">
        <span>{showAll ? "Full log" : current ? `Log: ${current.title}` : "Log"}</span>
        <button type="button" className="link" onClick={() => setShowAll(!showAll)}>{showAll ? "This step only" : "Full log"}</button>
      </div>
      <pre className="log" ref={logRef} role="log" aria-live="off" aria-label="Deploy log">
        {visible.slice(-600).map((l) => (
          <span key={l.i} className={`l-${l.level}`}>{l.text}{"\n"}</span>
        ))}
      </pre>
    </section>
  );
}

function ActionCard({ action }: { action: NonNullable<RunState["action"]> }) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const isUrl = action.command.startsWith("https://");
  const ready = action.fields.every((f) => values[f.name]?.trim());
  return (
    <form className="action" onSubmit={async (e) => {
      e.preventDefault(); setBusy(true); setErr("");
      try { await api.continueRun(values); } catch (ex) { setErr((ex as Error).message); setBusy(false); }
    }}>
      <h2>Your turn</h2>
      <p>{action.message}</p>
      {action.command ? (
        isUrl ? <p><a className="link" href={action.command} target="_blank" rel="noreferrer">Open {new URL(action.command).hostname}</a></p>
        : action.command.startsWith("#") ? <p className="mono">{action.command.slice(1).trim()}</p> : (
          <div className="cmd"><code>{action.command}</code><Copy text={action.command} label="command" /></div>
        )
      ) : null}
      {Object.keys(action.info).length ? (
        <dl className="info">
          {Object.entries(action.info).map(([k, v]) => (
            <div key={k}><dt>{k}</dt><dd><code>{v}</code><Copy text={v} label={k} /></dd></div>
          ))}
        </dl>
      ) : null}
      {action.fields.map((f) => (
        <Field key={f.name} id={`f-${f.name}`} label={f.label} secret={f.secret} value={values[f.name]}
               onChange={(v) => setValues({ ...values, [f.name]: v })} />
      ))}
      {err ? <p className="alert" role="alert">{err}</p> : null}
      <div className="nav"><span />
        <button type="submit" className="btn primary" disabled={busy || !ready}>Continue</button>
      </div>
    </form>
  );
}

function Done({ run }: { run: RunState }) {
  const r = run.result;
  return (
    <section className="step done">
      <LogoSweep />
      <h1>AI Scan Repos is running</h1>
      <p className="big-link"><a href={r.app_url} target="_blank" rel="noreferrer">{r.app_url}</a></p>
      <p className="lead">Sign in with GitHub.</p>
      <dl className="info">
        {Object.entries(r.commands ?? {}).map(([k, v]) => (
          <div key={k}><dt>{k}</dt><dd><code>{v}</code><Copy text={v} label={`${k} command`} /></dd></div>
        ))}
        {Object.entries(r.passwords ?? {}).map(([k, v]) => (
          <div key={"pw" + k}><dt>{k} login</dt><dd><Reveal user="admin" secret={v} label={k} /></dd></div>
        ))}
        {r.jenkins_url ? <div><dt>Jenkins address</dt><dd><code>{r.jenkins_url}</code><Copy text={r.jenkins_url} label="Jenkins address" /></dd></div> : null}
        <div><dt>Images</dt><dd><code>{r.registry}</code></dd></div>
      </dl>
    </section>
  );
}

function Reveal({ user, secret, label }: { user: string; secret: string; label: string }) {
  const [shown, setShown] = useState(false);
  return (
    <>
      <code>{user} / {shown ? secret : "••••••••"}</code>
      <button type="button" className="copy" onClick={() => setShown(!shown)}>{shown ? "Hide" : "Show"}</button>
      <Copy text={secret} label={`${label} password`} />
    </>
  );
}
