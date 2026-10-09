# Simulated runs: QA findings

Test file: `tests/test_simulated_runs.py`. Run it with
`"$TEMP/claude-instvenv/Scripts/python" -m pytest tests/test_simulated_runs.py -q`.
Result against the code as of 2026-10-09 19:20: **6 passed, 3 xfailed**, stable over 4 runs (~6 s).
`tests/test_backend.py` still passes too, with 9 tests.

`tests/test_simulated_runs_alt.py` comes from another writer and is not part of this report.
Right now it fails: one of its runs hangs, and every test after that one gets
"Another run is still going", because `runner._current` is never reset.

## How the simulation works

Each plan runs through `runner.start`, which gives it a real thread, a real log, real redaction and real
`wait_for_user` pauses. The test answers each pause by setting `run.reply` and then calling `run.resume.set()`.
These parts are faked: `Ctx.sh`, `kube.flavour`, `kube.subprocess` (the ssh call that reads k3s.yaml),
`github.ensure_webhook/delete_webhook(s)/new_smee_channel/file_text`, `httpx` (a fake Jenkins built on
MockTransport), `subprocess.Popen` (the port-forward) and `time` (a fake clock).

Only `kubectl kustomize` runs for real. It renders the overlays that the plan writes into copies of the
three deployment repos' working trees, untracked files included. `INSTALLER_DATA` and `TF_DIR` point to tmp dirs.

## What passed

- **a) local minikube, AI same_machine**
  - Command order: clone, ClusterIP registry 10.96.100.100, ingress-nginx, kustomize + commit/push,
    secrets, Jenkins, smee, Argo CD, health, result.
  - Pauses come in this order: `minikube tunnel`, then OAuth. The OAuth answer is saved with `for_url`.
  - Jenkins values contain REGISTRY_KIND=local and REGISTRY=10.96.100.100:5000.
  - Rendered overlays: the lmstudio Service is dropped, LMSTUDIO_BASE_URL is the endpoint, the ingress is nginx,
    the StorageClass copies minikube-hostpath without mountOptions, cookie_secure is false, and admins are written to the ConfigMap.
  - Three webhooks are created on the smee URL.
  - Jenkins gets crumb, scan and build, and builds are polled until SUCCESS.
  - The Argo CD password is captured.
- **b) SSH K3s with a sudo password, AI tailnet**
  - The kubeconfig is read with `sudo -S`, with the password on stdin only. The server is rewritten to the SSH host.
  - registries.yaml is written over ssh with sudo, and the password is never in argv. The test runs the real
    remote script through `sh` and checks that the file it writes parses to the expected mirror.
  - `get nodes` waits for K3s to come back afterwards.
  - Traefik is used (no ingress-nginx). The Tailscale operator gets its OAuth Secret and the repo's values.yaml.
  - The rendered lmstudio Service has the tailnet IP. The StorageClass keeps local-path and noexec.
  - A saved OAuth App for the same address means the run does not pause.
- **c) EKS with a domain, AI cloud**
  - Order: sts, init, apply, output, update-kubeconfig, `aws acm wait certificate-validated`,
    gp3, clone, ingress (with the ACM cert and NLB annotations), LB lookup, overlays, secrets,
    IRSA annotation, Jenkins twice (the second time with the public URL), Argo CD, health.
  - AWS credentials are passed only through env.
  - Pauses come in this order: the ACM record, the CNAME to the LB, then OAuth with https URLs.
  - Jenkins values contain REGISTRY_KIND=ecr, REGISTRY=<ECR> and AWS_REGION. There is no in-cluster registry and no smee.
  - Rendered overlays: the images are on ECR; the EFS StorageClass has the efs_id and no mountOptions; the PVC is RWX.
  - The gateway overlay has `https://scan.example.com`, `GATEWAY_COOKIE_SECURE: "true"`,
    preset/model, and the GATEWAY_DEFAULT_AI_KEY secretKeyRef. `default-ai-key` is in gateway-secrets.
  - The webhooks point at `http://<jenkins-lb>:8080/github-webhook/`.
- **d) destroy**
  - EKS order: update-kubeconfig, delete Applications, delete the app namespace, helm uninstalls
    (ingress-nginx is uninstalled, so the NLB is removed), delete namespaces and the StorageClass, terraform init, terraform destroy.
  - Local destroy does the same without terraform.
  - Both remove the three webhooks by their saved ids. For EKS this was tested after a real simulated install,
    which saves `webhook_ids` and `webhook_url`.
- **Every scenario**
  - No credential appears in any log line or step detail: token, sudo password, AWS secret, API key, TS secret,
    OAuth secret, private-key lines, the generated passwords and keys, or the Argo CD password.
  - No credential appears in any command's argv either.
  - Each run ends with status `done` and every step `done`.

## Bugs in installer/ (each kept as a strict xfail)

1. **kube.py:69 `_embed`: certificates are not actually embedded.**
   - The code does `obj[key.replace("-file", "-data")] = base64...`, but the keys are `certificate-authority`,
     `client-certificate` and `client-key`, and none of them contains `-file`. The base64 text is written back
     under the file-path key, so kubectl tries to open a file named `Q0E=...`.
   - Every local minikube install with file-path certificates breaks, which is the usual minikube setup on Windows and macOS.
   - Fix: `obj[key + "-data"] = base64.b64encode(...)`.
   - Test: `test_a2_local_kubeconfig_embeds_minikube_certs`.
2. **overlays.py, backend `lmstudio-patch.yaml` (around line 76-90): a custom tailnet port adds a second port.**
   - Strategic merge matches Service ports by `port`, so a port of 11434 renders as `[11434, 1234]`.
     Both ports are named `http`, so the API server rejects the Service when Argo CD syncs it.
   - Fix: use a JSON 6902 patch (`replace /spec/ports` with the single port), or add `$patch: replace` to the ports list.
   - Test: `test_b2_tailnet_custom_port_replaces_the_port`.
3. **runner.py, `Ctx.wait_for_user` (status/action are set before `reply = {}` / `resume.clear()`): a lost Continue.**
   - `app.run_continue` accepts a Continue once `status == "waiting"`. If one arrives inside that window, it is
     wiped, and the run then waits forever. The window is small, so humans rarely hit it, but a script or a double-click can.
   - Fix: reset `reply` and call `resume.clear()` first, then set `status`/`action` and call `touch()`.
   - Test: `test_runner_continue_right_after_pause_is_not_lost`.

## Smaller findings (no test)

- **The smee URL is printed in clear.**
  - `secret_values()` treats every string in `generated` as a secret, including `smee_url` and `webhook_url`.
    But a first install creates the smee URL after `run.secrets` has been fixed, so `s_webhooks` logs it in
    clear, and later runs redact it. Anyone who has the channel URL can read the webhook payloads.
  - Decide which behaviour is wanted:
    - To keep it secret, add `ctx.run.secrets.append(g["smee_url"])` in `s_webhooks` after creating it.
    - To treat it as public, exclude `smee_url`/`webhook_url` from `secret_values`.
  - The test currently excludes these two values from its no-secret check.
- **Old answer files leave their webhooks behind.**
  - Answers saved before the `webhook_ids`/`webhook_url` change only have `smee_url`.
    For them, `s_destroy_cluster_side` passes `hook_id=None, url=""`, so destroy leaves the old smee webhooks.
  - Fix: fall back to `g.get("smee_url")`.
- **On EKS, Jenkins is published as a `LoadBalancer` on port 8080.**
  - It is reachable from the internet with an admin login, and its webhooks carry no secret.
  - Consider restricting it with `loadBalancerSourceRanges` (GitHub hook IPs plus the user's IP), or routing
    `/github-webhook/` through ingress-nginx.
- **In cloud AI mode, `overlays.backend` sets `LMSTUDIO_BASE_URL=http://lmstudio:1234/v1` while also deleting the lmstudio Service.**
  - This is harmless only if review-api never calls it in cloud mode. Worth confirming in the backend.

## Independent second suite (set aside: `qa/set-aside/test_simulated_runs_alt.py`)

A second QA agent wrote the same scenarios a) to d) independently before this file existed. Its faking approach
was different: a pattern-matching fake `Ctx.sh`, a fake `httpx.Client` class, and a skewed fake clock.
The suite was set aside at 19:22 so that one suite is kept. Against the code as it was around 19:15, it agreed with
the findings above and found the same bugs on its own:

- `_embed` at kube.py:69.
- The `wait_for_user` race in runner.py.
- The smee URL logged in clear on a first install.
- Two issues that were fixed in plans.py during the run:
  - EKS destroy never removed the Jenkins webhooks (now fixed via `webhook_ids`/`webhook_url`).
  - Nothing waited for the ACM certificate before the NLB TLS listener (now fixed via `aws acm wait certificate-validated`).

Extra minor points, not covered above:

- **plans.py `s_destroy_connect_eks` sets `ctx.state["cluster_gone"]`, but nothing reads it.**
  - When the cluster is already gone, every kubectl and helm command of `s_destroy_cluster_side` still runs and fails.
    They use `check=False`, so the run stays harmless.
  - Fix: skip the cluster-side commands, but keep the webhook removal.
- **plans.py `s_terraform` shows the ACM DNS-record pause on every upgrade, even when the certificate is already issued.**
  - Fix: skip the pause when `aws acm describe-certificate` reports `ISSUED`.
- **`secret_values` also redacts `generated.webhook_url`.**
  - On EKS that value is the public Jenkins URL, so on later runs the "Webhook on ... -> ..." lines show `••••`.
    This is cosmetic.
