# Project Installer Image: design decisions

Chosen by Elad on 2026-10-09, one question at a time.

| # | Topic | Decision |
|---|---|---|
| 1 | Form factor | One Docker image: `docker run`, open `http://localhost:8088`, the wizard does the rest. Contains kubectl, helm, kustomize, terraform, aws cli, git, ssh. |
| 2 | Code home | New repo `DevOpsCourse22/ai-scanner-installer`. |
| 3 | Architectures | amd64 + arm64 (buildx). |
| 4 | Images | Built exactly as today (Jenkins + BuildKit). Local: the in-cluster registry. AWS: ECR. |
| 4a | Local scope | Full platform, same as K3S-SRV: registry, Jenkins (Helm) with the 3 pipelines, smee, Argo CD, the app. |
| 4b | Reaching the target | Localhost: the local kube context (minikube, Docker Desktop). Remote server: host + SSH user; kubeconfig read over SSH. |
| 5 | Environments | Kustomize overlays in each deployment repo: `overlays/local`, `overlays/eks`. Jenkins edits its environment's overlay; each Argo CD Application points at its overlay. One branch, `main`. |
| 5a | Which repos | Default DevOpsCourse22; the wizard lets the user enter another owner (a fork). One GitHub token with repo write. |
| 6 | Local clusters | Any kube context; tested on minikube and K3s. Traefik kept on K3s, ingress-nginx elsewhere. |
| 7 | CI/CD | Full platform (see 4a, A). |
| 8 | AWS infra (Terraform) | New VPC, EKS with 2 x t3.large managed nodes, EBS CSI, EFS for the reports PVC (RWX), ECR repos for the 4 images, ingress-nginx on an NLB. Postgres in-cluster on EBS. |
| 9 | Terraform state | Local file on a mounted Docker volume. |
| 10 | AWS credentials | Both: a mounted `~/.aws` (pick a profile) or keys typed in the wizard. |
| 11 | EKS address | User picks: NLB hostname over http by default; optional domain for HTTPS (ACM). |
| 12 | Teardown | Destroy button with typed confirmation (namespaces locally, `terraform destroy` on EKS). |
| 13 | AI model choices | (1) local model on the same machine/server, (2) local model on a tailnet (Tailscale operator), (3) cloud provider + key. |
| 14 | Applying the default | Gateway change: on first start, if no AI setting is saved, seed it from env vars + a Secret. Admins can still change it. |
| 15 | Wizard stack | FastAPI backend (runs commands, streams logs) + React/Vite page styled like the app. |
| 16 | Secrets | Kept in memory and turned straight into K8s Secrets / Jenkins credentials; also saved in an answers file on the Docker volume, encrypted with a passphrase the user types. Postgres password, settings-key and webhook secret are generated. |
| 17 | Flow | Linear steps, one line of text each: target, repos + token, AI model, GitHub OAuth App (exact URLs), first admin, review, deploy with live logs, app URL + health check. |
| 18 | Re-runs | Detects an existing install; offers Upgrade or Destroy. |
| A | EKS platform | Full platform on EKS too: Jenkins with `REGISTRY_KIND=ecr` and IRSA, Argo CD, webhook straight to Jenkins's load balancer (no smee). |
| B | First build | The installer triggers the 3 Jenkins pipelines and shows progress; Argo CD deploys when they commit the tags. |
| C | Webhooks | Created automatically with the GitHub token: a new smee.io channel locally, the Jenkins LB URL on EKS. |

Manual steps the wizard cannot do for the user: create the GitHub OAuth App (GitHub has no API for it),
create the GitHub token, and (tailnet option) the Tailscale OAuth client and policy.
