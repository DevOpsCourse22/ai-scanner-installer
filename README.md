# AI Scan Repos installer

One Docker image that deploys AI Scan Repos with its full platform (image
registry, Jenkins with the three pipelines, GitHub webhooks, Argo CD) to:

- a cluster on your computer (minikube, Docker Desktop, any kube context),
- a server you can SSH into (K3s or kubeadm),
- Amazon EKS, created with Terraform (VPC, EKS, EFS, ECR).

A web wizard asks for what it cannot know, and tells you the few things only
you can do (a GitHub token, a GitHub OAuth App, `minikube tunnel`).

## Before you start

- Docker.
- A GitHub token that can read the six project repositories, add webhooks to the three code
  repositories and push to this one, created at
  [github.com/settings/tokens](https://github.com/settings/tokens/new?scopes=repo,admin:repo_hook&description=ai-scan-repos-installer)
  with `repo` and `admin:repo_hook`.
- For minikube: a cluster with at least 8 GB of memory, or the Jenkins builds starve it. A separate
  profile leaves your other clusters alone: `minikube start -p ai-scan --cpus 6 --memory 10g --container-runtime=docker`
  (then run the installer with `--network ai-scan`, pick the context `ai-scan`, and use `minikube tunnel -p ai-scan`).
- For EKS: an AWS account; either a `~/.aws` profile or access keys.

## Run it

Windows (PowerShell), minikube:

```powershell
docker run --rm -it -p 127.0.0.1:8088:8088 -p 127.0.0.1:8081-8083:8081-8083 --network minikube `
  -v ai-scan-installer:/data `
  -v "$env:USERPROFILE\.kube:/host/kube:ro" -v "$env:USERPROFILE\.minikube:/host/minikube:ro" `
  -v "$env:USERPROFILE\.aws:/root/.aws:ro" `
  ghcr.io/devopscourse22/ai-scanner-installer:latest
```

macOS / Linux (bash):

```bash
docker run --rm -it -p 127.0.0.1:8088:8088 -p 127.0.0.1:8081-8083:8081-8083 --network minikube \
  --add-host host.docker.internal:host-gateway \
  -v ai-scan-installer:/data \
  -v "$HOME/.kube:/host/kube:ro" -v "$HOME/.minikube:/host/minikube:ro" -v "$HOME/.aws:/root/.aws:ro" \
  ghcr.io/devopscourse22/ai-scanner-installer:latest
```

Then open <http://localhost:8088>.

- Leave out `--network minikube` for any target other than minikube.
- Start minikube **before** the installer. If the installer joins a stopped minikube's network,
  it takes the node's address and minikube then fails with "Address already in use".
- Leave out the `.aws` mount if you will type AWS keys in the wizard, or are not using EKS.
- The volume `ai-scan-installer` keeps your encrypted answers and the Terraform
  state. Keep it until you have pressed Destroy, or EKS keeps costing money.

## What it does

The six project repositories are only **read**. Everything an install writes
goes to a branch of this repository, `env/local` or `env/eks`:

```
env/<env>
└── environments/<env>/
    ├── backend/   base/ (copy of ai-scanner-backend-deployment) + overlay patches
    ├── gateway/   base/ (copy of ai-scanner-gateway-deployment) + overlay patches
    ├── frontend/  base/ (copy of ai-scanner-frontend-deployment) + overlay patches
    └── ci/        <repo>.Jenkinsfile + <repo>.pod.yaml, derived from each code repo
```

| Step | Local / server | EKS |
|---|---|---|
| Cluster | your kube context, or the server's admin kubeconfig over SSH | Terraform: VPC, EKS (2 x t3.large), EBS + EFS CSI, EFS, 4 ECR repos, IAM role for Jenkins |
| Images | a registry inside the cluster | ECR |
| Ingress | Traefik on K3s, the existing nginx class, or ingress-nginx | ingress-nginx on an AWS NLB |
| CI | Jenkins: one job per code repo, using the derived Jenkinsfile; webhook through smee.io | same, webhook straight to Jenkins's load balancer |
| CD | Argo CD syncs `environments/<env>/<app>` from the `env/<env>` branch | same |
| AI model | saved once through the gateway's own Settings code | same |

The derived Jenkinsfiles (`installer/pipelines.py`) keep the original Test,
Build, Scan and Push stages. They differ only in checking out the code repo
themselves and committing image tags to `env/<env>` here. The first images come
from the first Jenkins run.

The only thing an install adds to the project repositories is a push webhook
on the three code repositories (a repository setting, not a commit). Remove
deletes it again.

## Jenkins, Argo CD and Grafana

Press **Jenkins, Argo CD, Grafana** at the top of the wizard, then **Open all three**.
The installer keeps a port-forward to each one, for any target (minikube, a server,
EKS), on ports the `docker run` line publishes on 127.0.0.1 only:

| Service | Address | Login |
|---|---|---|
| Jenkins | http://localhost:8081 | `admin` + the install's admin password |
| Argo CD | http://localhost:8082 | same |
| Grafana (with Prometheus) | http://localhost:8083 | same |

The wizard shows the password (Show / Copy). It is generated once per install and
kept in the encrypted answers; Argo CD stores only its bcrypt hash.

## Secrets

Everything you type stays in the installer's memory and in `answers.enc` on
the volume, encrypted with your passphrase (scrypt + AES-GCM). Inside the
cluster they become Kubernetes Secrets. The log never shows them. Nothing is
written to git except the overlays, which hold no secrets.

## Developing

```bash
pip install -r requirements-dev.txt && pytest -q
uvicorn installer.app:app --port 8088        # the API
cd ui && npm install && npm run dev           # the page, proxied to the API
```

Build for both architectures: `docker buildx build --platform linux/amd64,linux/arm64 -t <name> --push .`
