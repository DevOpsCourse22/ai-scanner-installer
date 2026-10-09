# Installer: build plan and progress log

Read this first when resuming. Decisions: `DESIGN-DECISIONS.md`. Update the checklist as you go.

## Architecture

```
ai-scanner-installer/
  Dockerfile               multi-stage: node builds ui/ ; python:3.12-slim + kubectl, helm, terraform, aws cli, git, ssh
  installer/               Python package, FastAPI (port 8088)
    app.py                 HTTP API + serves the built UI
    vault.py               answers file /data/answers.enc (scrypt + AES-GCM, passphrase typed by the user)
    runner.py              runs a plan of steps, streams log lines over SSE, redacts secrets
    target.py              kube access: local kubeconfig (mounted ~/.kube) or SSH (reads kubeconfig, rewrites server address)
    detect.py              cluster flavour (k3s / minikube / eks / other), existing install
    plan_local.py          steps for local / server installs
    plan_eks.py            steps for EKS (terraform, then the same platform steps with ECR + IRSA)
    github.py              token check, webhooks, smee channel
    manifests/             platform templates (registry, smee, argocd apps, jenkins values) rendered with string.Template
  terraform/eks/           VPC, EKS, EBS CSI, EFS + StorageClass, ECR x4, IRSA role for Jenkins
  ui/                      React + Vite wizard
```

## How a local install works (decided)

1. Target: `local` uses the mounted kubeconfig context; `ssh` copies `/etc/rancher/k3s/k3s.yaml` (or `~/.kube/config`) over SSH and rewrites `server:` to the SSH host.
2. Registry: Deployment + Service in namespace `registry`.
   - minikube (docker runtime): Service with fixed `clusterIP: 10.96.100.100`, port 5000. minikube's Docker trusts 10.96.0.0/12 as insecure by default, and Jenkins pods reach the ClusterIP. REGISTRY=10.96.100.100:5000
   - k3s: NodePort 30500, REGISTRY=<nodeIP>:30500, `registries.yaml` written over SSH (sudo) + `systemctl restart k3s`; without SSH the wizard shows the command to run.
   - other: NodePort 30500; wizard shows how to trust it.
3. Jenkins via Helm (values rendered from template: REGISTRY_KIND/REGISTRY, DEPLOY_OWNER, DEPLOY_ENV, credentials from Secrets, multibranch jobs via job-dsl in JCasC).
4. smee: new channel (POST https://smee.io/new -> Location), Secret `smee-channel`, smee-client Deployment.
5. Argo CD via Helm; repo credentials Secret with the token; 3 Applications pointing at `overlays/<env>` of the chosen owner's deployment repos.
6. App Secrets (postgres-credentials, gateway-secrets incl. default AI seed), ConfigMap gateway-admins.
7. Webhooks on the 3 code repos (GitHub API).
8. Trigger the 3 Jenkins jobs (scan multibranch), wait for Argo CD Healthy, health check.

## Changes needed in other repos (do on branch `installer-support`)

- Code repos' Jenkinsfile: `DEPLOY_REPO = "${env.DEPLOY_OWNER ?: 'DevOpsCourse22'}/ai-scanner-<x>-deployment"`, and the kustomization path `overlays/${env.DEPLOY_ENV}` when DEPLOY_ENV is set (else today's root file, so K3S-SRV keeps working).
- Deployment repos: `overlays/local`, `overlays/eks` (base = today's root). Public address, storage class, registry name patched in the overlay.
- Gateway: seed site AI setting at first start from `GATEWAY_DEFAULT_AI_*` env + Secret key.

## Checklist

- [x] Design decisions
- [x] gateway AI seed: ai-scanner-gateway branch `installer-support` (uncommitted; 101 tests pass)
- [x] deployment repos, branch `installer-support` (uncommitted): per-folder kustomization.yaml units
      (backend: storage/ postgres/ review-api/ ingress/; gateway+frontend: files moved to app/). Overlays
      can't live inside the folder they build on (kustomize cycle error), hence the units.
- [x] code repos Jenkinsfile + ci/deploy-lib.sh, branch `installer-support`: DEPLOY_OWNER, DEPLOY_ENV
      (set_image edits overlays/<env>/app-patch.yaml). Tested with generated overlays.
- [x] installer/overlays.py (verified with kubectl kustomize), vault.py, runner.py, kube.py, github.py
- [x] installer/ backend: app.py, plans.py, platform.py (9 unit tests pass: tests/test_backend.py)
- [x] terraform/eks (not yet `terraform validate`d: no terraform on the laptop; validate inside the image)
- [x] ui/ (React+Vite, builds; logo Lottie from ui/scripts/make_logo_lottie.py on the Done screen)
- [x] Dockerfile (local build OK, 1.07 GB; terraform validate passes inside it). README.md has docker run lines.
- [ ] QA: static review + simulated EKS run (subagents), then live minikube test (needs Elad's OK: it commits overlays and adds webhooks on GitHub)
