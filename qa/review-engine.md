# QA review: deploy engine (plans, platform, overlays, kube, github, runner, app)

Static review on 2026-10-09. Nothing was run against a cluster, AWS or GitHub.
Tests: `pytest -q tests` gave **9 passed** (1 starlette deprecation warning).
Line numbers are as read during the review. plans.py and app.py were edited by someone else while it ran, so numbers may be off by a line or two.
Findings are ordered from most to least severe.

## 1. CRITICAL: minikube (and clusters with no LB) time out at the ingress step
- **Where:** `installer/plans.py:237-240` runs `helm upgrade --install ingress-nginx ... --wait --timeout 10m` for a Service of type LoadBalancer. The `minikube tunnel` prompt comes only later, at `plans.py:262-266` (`s_address`).
- **What breaks:** Helm 3 `--wait` (the Dockerfile pins v3.19.0) treats a LoadBalancer Service as not ready until `status.loadBalancer.ingress` is set. On minikube that needs `minikube tunnel`, which the wizard asks for one step later. So every minikube install fails at "ingress" after 10 minutes. Kubeadm or bare-metal clusters ("other") with no LB provider fail the same way.
- **Fix (either one):**
  - Ask for `minikube tunnel` before the ingress Helm install.
  - Or drop `--wait` for non-EKS clusters and run `kubectl -n ingress-nginx rollout status deploy/ingress-nginx-controller` instead. For "other", use `controller.service.type=NodePort`.
- **Verified:** helm v3.19.0 `pkg/kube/ready.go` `serviceReady` returns false while `Status.LoadBalancer.Ingress == nil` and there are no externalIPs: https://github.com/helm/helm/blob/v3.19.0/pkg/kube/ready.go

## 2. HIGH: `ensure_webhook` takes over unrelated webhooks that already exist (breaks K3S-SRV CI)
- **Where:** `installer/github.py:57`, the match `old == url or "smee.io" in old or old.endswith("/github-webhook/")`.
- **What breaks:** With the default owner DevOpsCourse22, the code repos already carry K3S-SRV's smee or Jenkins hook. The installer PATCHes that hook to point at its new smee channel, so the existing K3S-SRV Jenkins stops getting pushes. Destroy (`plans.py:~608-614`) deletes only hooks whose URL equals the new smee URL, so the original hook is never restored. The EKS Jenkins hook is never deleted at all.
- **Fix:**
  - Update a hook only when its URL equals `url`, or equals a URL this installer saved before (`generated.smee_url`, or a saved EKS `jenkins_url`). Otherwise create a new hook.
  - Save the EKS webhook URL in the answers and delete it on destroy.
- **Verified:** by reading the code.

## 3. HIGH: destroy can leave `ai-scan-repos` stuck in Terminating (tailnet mode)
- **Where:** `installer/plans.py:~602-606`. The Helm releases, including `tailscale-operator`, are uninstalled before the namespaces are deleted.
- **What breaks:** The Tailscale operator puts a finalizer on the egress Service `lmstudio`. With the operator already gone, nothing removes that finalizer, so deleting `ai-scan-repos` hangs. The step still "succeeds" because of `check=False`. The next install then fails: nothing can be created in a Terminating namespace.
- **Fix:**
  - Delete the `ai-scan-repos` namespace and wait for it to go before uninstalling `tailscale-operator` (and, to be safe, before Argo CD and ingress-nginx).
  - As a fallback, patch out the finalizers on Services left in that namespace.
- **Suspected:** based on how the operator uses finalizers. Not reproduced.

## 4. MEDIUM: the overlay push races Jenkins on an upgrade and can revert image tags
- **Where:**
  - `installer/plans.py:331-333`: clone, or `pull --rebase`.
  - `plans.py:370-377`: `keep_tag` reads the current tags.
  - `plans.py:409`: `git push origin HEAD:main`.
- **What breaks:** On an upgrade, Jenkins and its webhooks are live. Between `s_clone` and `s_overlays` the run waits for the user (the tunnel and OAuth prompts), which can take minutes.
  - If Jenkins commits a new tag in that window, the push is rejected as non-fast-forward and the step fails.
  - `keep_tag` reads from the stale clone, so it can write an older tag back.
- **Fix:** Run `git pull --rebase` at the start of `s_overlays`, before `keep_tag`. Retry the push up to 3 times with `pull --rebase` in between, the same pattern as `ci/deploy-lib.sh`.
- **Verified:** by reading the code and the step order in `install_plan`.

## 5. MEDIUM: EKS with a domain: the NLB TLS listener gets an ACM certificate that is not issued yet
- **Where:** `installer/plans.py:171-178` shows the validation CNAME and then carries on. `plans.py:235-240` passes the ARN to ingress-nginx.
- **What breaks:** The step's own text says the certificate is issued "a few minutes later". While it is PENDING_VALIDATION, the AWS cloud provider cannot create the TLS listener. The Service gets no address, and `helm --wait` (10 minutes) can run out, especially with slow DNS.
- **Fix:** After the user presses Continue, poll `aws acm describe-certificate --certificate-arn <arn> --query Certificate.Status` until it returns `ISSUED`. Use a timeout of about 30 minutes with a clear message.
- **Suspected.**

## 6. MEDIUM: a run that is waiting for the user cannot be cancelled
- **Where:**
  - `installer/runner.py:138`: `resume.wait()` has no timeout and no abort.
  - `runner.py:162`: a new run is refused while one is `waiting`.
  - `app.py` has no cancel endpoint.
- **What breaks:** If a manual step cannot be done (for example the OAuth App or the DNS record), the only way out is restarting the container, which also drops `WORKDIR`.
- **Fix:** Add `POST /api/run/cancel`. It sets `run.cancelled = True` and calls `resume.set()`, and `wait_for_user` then raises `StepFailed("Cancelled by the user")`.
- **Verified:** by reading the code.

## 7. MEDIUM: the IRSA annotation can race the creation of the `default` ServiceAccount
- **Where:** `installer/plans.py:442-449`. `ensure_ns("jenkins")` runs, then a second or so later `kubectl annotate serviceaccount default` with `check=True`.
- **What breaks:** The controller-manager creates the `default` ServiceAccount asynchronously. On a fresh cluster the annotate can return NotFound and fail the step.
- **Fix (either one):**
  - `wait_until(ctx, "the default ServiceAccount", lambda: ctx.sh(k(ctx)+["-n","jenkins","get","sa","default"], quiet=True), 60, 2)` before annotating.
  - Or `kubectl apply` the ServiceAccount `default` with the annotation already on it.
- **Suspected.**

## 8. LOW-MEDIUM: edge cases in `s_first_build`
- **Where:** `installer/plans.py:531-552`.
- **Old builds count as this run's result:** on an upgrade with no new commit, `lastBuild` is an old build. An old FAILURE fails the step at once; an old SUCCESS passes without any build. **Fix:** record `job/<x>/job/main` `nextBuildNumber` before the scan, then wait for that build number or a later one.
- **Error responses pass the crumb check:** a 401 or 403 that returns any truthy dict passes the crumb `wait_until`, and the next line then raises `KeyError` on `crumb["crumbRequestField"]`. **Fix:** check `r.status_code == 200` inside the lambda.
- **The scan trigger is never checked:** the response of `j.post(.../build)` is ignored. **Fix:** call `raise_for_status()` on it.
- **Verified:** by reading the code.

## 9. LOW: Secret values are copied into the `last-applied-configuration` annotation
- **Where:** `installer/plans.py:84`, client-side `kubectl apply -f -` of the Secret JSON.
- **What breaks:** client-side apply stores the full object, base64 `data` included, in the `kubectl.kubernetes.io/last-applied-configuration` annotation. The GitHub token, OAuth secret and AI key therefore sit in a second place on each Secret. They do not leak to the log or to git.
- **Fix:** `kubectl apply --server-side --force-conflicts --field-manager=ai-scanner-installer -f -`.
- **Verified:** this is how kubectl client-side apply is documented to work.

## 10. LOW: `delete_webhooks` crashes when GitHub does not return a list
- **Where:** `installer/github.py:70`. On a 404 or 403, `gh.get(...).json() or []` is a dict. Looping over it gives strings, and `h.get` raises `AttributeError`. `plans.py` catches only `httpx.HTTPError`, so the destroy step fails.
- **Fix:** guard it with `isinstance(hooks, list)`, as `ensure_webhook` already does.
- **Verified:** by reading the code.

## 11. LOW: K3s with agent nodes; minikube with a custom service range
- **K3s agent nodes:** `installer/plans.py:212-219` writes `registries.yaml` only on the SSH host. Pods scheduled on K3s agent nodes cannot pull from the plain-HTTP registry. **Fix:** when there is more than one node, show the command to run on each one. **Suspected.**
- **Custom minikube service range:** `installer/platform.py:20` hard-codes the registry's ClusterIP. If minikube was started with a non-default `--service-cluster-ip-range`, `clusterIP: 10.96.100.100` is rejected, and the runtime would not trust it anyway. **Fix:** read the range from the `kubernetes` Service's ClusterIP and pick an address inside it. If it is outside 10.96.0.0/12, fail with a clear message. **Suspected.**

## Checked and OK
- **jenkins_values against chart 5.9.x.** Verified against https://github.com/jenkinsci/helm-charts/blob/main/charts/jenkins/values.yaml and `templates/jcasc-config.yaml`.
  - `admin.existingSecret`: the chart's default keys `jenkins-admin-user` and `jenkins-admin-password` match `plans.py:443`.
  - `additionalExistingSecrets`: each secret is mounted as `<name>-<keyName>`, so `${jenkins-github-username}` and `${jenkins-github-token}` are the right references.
  - `configScripts`: the chart renders these through `tpl`. The scripts contain no `{{`, so the single Groovy braces are safe.
  - The `ci-jobs` block was rendered and loaded with PyYAML; the `jobs:` / `script: |` structure parses.
  - The job-dsl definitions match GitHubSCMSource's `@DataBoundConstructor` and symbols:
    - `multibranchPipelineJob` with `branchSources.branchSource.source.github`, setting `id`, `repoOwner`, `repository`, `repositoryUrl`, `configuredByUrl` and `credentialsId`.
    - traits `gitHubBranchDiscovery` and `gitHubPullRequestDiscovery`.
    - Still to do: watch the JCasC log during the live test.
  - The JCasC syntax `credentials.system.domainCredentials[].credentials[].usernamePassword` is correct.
- **Argo CD 8.x.**
  - `configs.params."server.insecure"`, `dex.enabled` and `notifications.enabled` are valid chart keys.
  - The repository Secret has the label `argocd.argoproj.io/secret-type: repository` and the keys `type`, `url`, `username` and `password`, as in the declarative-setup docs.
  - The Application spec is valid.
- **ingress-nginx TLS on the NLB.** The settings match the ingress-nginx recipe for TLS termination on an AWS NLB:
  - `aws-load-balancer-type: nlb`
  - `ssl-cert`, and `ssl-ports: "443"`
  - `backend-protocol: tcp`
  - `targetPorts.https: http`

  The public subnets carry the `kubernetes.io/role/elb` tag (`terraform/eks/main.tf:52`).
- **Helm values on stdin.** `helm ... -f -` reads stdin in Helm 3. No secret appears on any command line; Secrets are sent on stdin.
- **minikube registry.** The `minikube start --insecure-registry` docs say the default service CIDR (10.96.0.0/12) is added automatically (https://minikube.sigs.k8s.io/docs/commands/start/). The node reaches ClusterIPs through the kube-proxy rules, and BuildKit pushes with `registry.insecure=true` (backend `Jenkinsfile:110`).
- **host.minikube.internal.** It is documented only as a node `/etc/hosts` entry (https://minikube.sigs.k8s.io/docs/handbook/host-access/). Recent minikube versions also add it to CoreDNS, but that is not documented. In the live test, check that it resolves from a pod.
- **Build pods and IRSA.** `ci/pod.yaml` sets no `serviceAccountName`. The chart's `agent.namespace` defaults to the release namespace, and `serviceAccountAgent.name` falls back to `default`. So build pods run as `jenkins:default`, which matches:
  - the annotation at `plans.py:448`;
  - the IRSA trust `jenkins:default` (`terraform/eks/main.tf:176`);
  - the ECR repository names, which equal the image names.
- **Redaction and secrets.**
  - Commands and their output go through `Run.redact`.
  - Secret wizard fields and the Argo CD password are added to the redaction list.
  - The git token is passed through askpass plus an env var, never in a URL or in `.git/config`.
  - The overlays committed to git contain no secrets: the AI key is a `secretKeyRef`.

## Not covered (left for the live test)
- How long JCasC indexing and the first scan take.
- The `s_healthy` probe assumes `python` exists in the gateway image and that the gateway serves `/health` on 8080.
- Sizing: minikube likely needs `--cpus 4 --memory 8g` or more for Jenkins, Argo CD, BuildKit and the app. Put this in the README.
