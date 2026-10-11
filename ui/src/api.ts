// The installer API (installer/app.py). Same origin, so no CORS and no tokens.

export type Answers = {
  target?: {
    kind?: "local" | "ssh" | "eks";
    context?: string;
    ssh?: { host?: string; user?: string; port?: number; private_key?: string; private_key_saved?: boolean;
            sudo_password?: string; sudo_password_saved?: boolean };
    eks?: { region?: string; name?: string; aws_profile?: string; aws_access_key_id?: string;
            aws_secret_access_key?: string; aws_secret_access_key_saved?: boolean;
            domain?: string; node_type?: string; node_count?: number; admin_arns?: string };
  };
  repos?: { owner?: string; installer_repo?: string; token?: string; token_saved?: boolean; login?: string };
  ai?: { mode?: "same_machine" | "tailnet" | "cloud"; endpoint?: string; model?: string; preset?: string;
         api_key?: string; api_key_saved?: boolean; tailnet_ip?: string; tailnet_port?: number;
         ts_client_id?: string; ts_client_secret?: string; ts_client_secret_saved?: boolean };
  public_url?: string;
  admins?: string[];
  // the last finished deploy, remembered across installer restarts (no passwords)
  last_result?: { app_url?: string; jenkins_url?: string; registry?: string; kind?: string; finished?: string;
                  target?: { kind?: string; context?: string; host?: string; region?: string } };
};

export type RunStep = { id: string; title: string; status: string; detail: string };
export type RunAction = { message: string; command: string; step: string;
  fields: { name: string; label: string; secret: boolean }[]; info: Record<string, string> };
export type RunState = { kind: string; status: string; action: RunAction | null; steps: RunStep[];
  result: { app_url?: string; jenkins_url?: string; webhook?: string; registry?: string;
            commands?: Record<string, string>; passwords?: Record<string, string> } };
export type LogLine = { i: number; step: string; level: string; text: string };

export type Status = {
  vault_exists: boolean; unlocked: boolean; default_owner: string; minikube_mounted: boolean;
  contexts: { name: string; server: string; current: boolean }[];
  aws_profiles: string[]; run: RunState | null;
};

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method, headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${r.status})`);
  return data as T;
}

export const api = {
  status: () => call<Status>("GET", "/api/status"),
  unlock: (passphrase: string, isNew: boolean) =>
    call<{ answers: Answers }>("POST", "/api/unlock", { passphrase, new: isNew }),
  getAnswers: () => call<{ answers: Answers }>("GET", "/api/answers"),
  checkAnswers: () => call<{ missing: string[] }>("GET", "/api/check/answers"),
  save: (answers: Answers) => call<{ answers: Answers }>("PUT", "/api/answers", { answers }),
  checkGithub: (owner: string, token: string) => call<{
    ok: boolean; login?: string; error?: string; problems?: string[];
    repos?: { name: string; found: boolean; private: boolean | null; push: boolean; admin: boolean }[];
  }>("POST", "/api/check/github", { owner, token }),
  checkTarget: () => call<{ installed: boolean; error?: string; flavour?: { kind: string; node_ip: string } }>(
    "POST", "/api/check/target"),
  checkModel: (endpoint: string) => call<{ ok: boolean; models?: string[]; error?: string }>(
    "POST", "/api/check/model", { endpoint }),
  run: (kind: string, confirm = "") => call<RunState>("POST", "/api/run", { kind, confirm }),
  getRun: () => call<RunState | Record<string, never>>("GET", "/api/run"),
  cancelRun: () => call("POST", "/api/run/cancel"),
  continueRun: (values: Record<string, string>) => call("POST", "/api/run/continue", { values }),
};
