"""Manifests and Helm values for the platform around the app: the image
registry, Jenkins, smee, Argo CD and the Argo CD Applications.

These mirror what ai-scanner-backend-deployment/platform/ and
CI-PIPELINES.md set up by hand on K3S-SRV, with the per-install values
(registry address, repo owner, environment, addresses) filled in.
"""

from __future__ import annotations

import json
from typing import Any

import yaml

JENKINS_CHART = "5.9.68"        # same as platform/jenkins/values.yaml
ARGOCD_CHART = "8.6.0"
INGRESS_NGINX_CHART = "4.13.3"
TAILSCALE_CHART = "1.102.4"     # same as DEPLOY-K3S.md
REGISTRY_CLUSTER_IP = "10.96.100.100"   # inside minikube's default service range, which its Docker trusts as insecure


def registry(mode: str) -> str:
    """mode: "clusterip" (minikube) or "nodeport" (K3s and others)."""
    svc = (f"  type: ClusterIP\n  clusterIP: {REGISTRY_CLUSTER_IP}\n  ports:\n"
           "    - port: 5000\n      targetPort: 5000\n") if mode == "clusterip" else (
           "  type: NodePort\n  ports:\n    - port: 5000\n      targetPort: 5000\n      nodePort: 30500\n")
    return f"""apiVersion: v1
kind: Namespace
metadata:
  name: registry
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: registry-data
  namespace: registry
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 30Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: registry
  namespace: registry
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels: {{app: registry}}
  template:
    metadata:
      labels: {{app: registry}}
    spec:
      containers:
        - name: registry
          image: registry:2.8.3
          ports:
            - containerPort: 5000
          env:
            - name: REGISTRY_STORAGE_DELETE_ENABLED
              value: "true"
          volumeMounts:
            - name: data
              mountPath: /var/lib/registry
          readinessProbe:
            httpGet: {{path: /v2/, port: 5000}}
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: registry-data
---
apiVersion: v1
kind: Service
metadata:
  name: registry
  namespace: registry
spec:
  selector: {{app: registry}}
{svc}"""


def k3s_registries(registry_addr: str) -> str:
    """/etc/rancher/k3s/registries.yaml: pull from our registry over plain HTTP."""
    return yaml.safe_dump({"mirrors": {registry_addr: {"endpoint": [f"http://{registry_addr}"]}}})


def jenkins_values(o: dict[str, Any]) -> str:
    """o: registry_kind, registry, owner, env, jenkins_url, aws_region, service_type,
    installer_repo, branch. One pipeline job per code repository; its Jenkinsfile is
    the installer's derived copy on the install's branch (installer/pipelines.py)."""
    env = [("REGISTRY_KIND", o["registry_kind"]), ("REGISTRY", o["registry"]),
           ("CODE_OWNER", o["owner"]), ("INSTALLER_REPO", o["installer_repo"]),
           ("DEPLOY_BRANCH", o["branch"]), ("DEPLOY_ENV", o["env"])]
    if o.get("aws_region"):
        env.append(("AWS_REGION", o["aws_region"]))
    jobs = "\n".join(f"""          pipelineJob('{repo}') {{
            description('Builds {o['owner']}/{repo} main; deploys to {o['installer_repo']} {o['branch']}')
            properties {{
              pipelineTriggers {{ triggers {{ githubPush() }} }}
            }}
            definition {{
              cpsScm {{
                scm {{
                  git {{
                    remote {{
                      url('https://github.com/{o['installer_repo']}.git')
                      credentials('github-deploy')
                    }}
                    branch('*/{o['branch']}')
                  }}
                }}
                scriptPath('environments/{o['env']}/ci/{repo}.Jenkinsfile')
                lightweight(true)
              }}
            }}
            logRotator {{ numToKeep(30) }}
          }}""" for repo in ("ai-scanner-backend", "ai-scanner-gateway", "ai-scanner-frontend"))
    values = {
        "controller": {
            "admin": {"existingSecret": "jenkins-admin"},
            "jenkinsUrl": o["jenkins_url"],
            "serviceType": o.get("service_type", "ClusterIP"),
            "additionalPlugins": ["github", "junit", "pipeline-graph-view", "job-dsl"],
            "additionalExistingSecrets": [
                {"name": "jenkins-github", "keyName": "username"},
                {"name": "jenkins-github", "keyName": "token"}],
            "JCasC": {"configScripts": {
                "ci-registry": yaml.safe_dump({"jenkins": {"globalNodeProperties": [{"envVars": {
                    "env": [{"key": k, "value": v} for k, v in env]}}]}}),
                "ci-credentials": yaml.safe_dump({"credentials": {"system": {"domainCredentials": [{
                    "credentials": [{"usernamePassword": {
                        "scope": "GLOBAL", "id": "github-deploy",
                        "description": "GitHub token (installer)",
                        "username": "${jenkins-github-username}",
                        "password": "${jenkins-github-token}"}}]}]}}}),
                "ci-jobs": "jobs:\n  - script: |\n" + jobs + "\n",
            }},
        },
        "persistence": {"size": "20Gi"},
    }
    return yaml.safe_dump(values, sort_keys=False)


def smee_client() -> str:
    """Same Deployment as platform/jenkins/smee-client.yaml."""
    return """apiVersion: apps/v1
kind: Deployment
metadata:
  name: smee-client
  namespace: jenkins
spec:
  replicas: 1
  selector:
    matchLabels: {app: smee-client}
  template:
    metadata:
      labels: {app: smee-client}
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: smee-client
          image: node:22-alpine
          command: ["sh", "-c"]
          args:
            - >-
              exec npx --yes smee-client@5.0.0
              --url "$SMEE_URL"
              --target "http://jenkins.jenkins.svc.cluster.local:8080/github-webhook/?"
          env:
            - name: SMEE_URL
              valueFrom:
                secretKeyRef: {name: smee-channel, key: url}
            - name: HOME
              value: /tmp
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: {drop: ["ALL"]}
          volumeMounts:
            - {name: tmp, mountPath: /tmp}
          resources:
            requests: {cpu: 10m, memory: 64Mi}
            limits: {memory: 256Mi}
      volumes:
        - name: tmp
          emptyDir: {}
"""


def argocd_values(admin_password: str, public: bool = False) -> str:
    """admin_password: the install's one admin password; Argo CD stores only its bcrypt hash.
    public: on EKS the UI gets its own AWS load balancer (http, admin login required)."""
    import bcrypt
    hashed = bcrypt.hashpw(admin_password.encode(), bcrypt.gensalt()).decode()
    values: dict[str, Any] = {"configs": {"params": {"server.insecure": True},
                                          "secret": {"argocdServerAdminPassword": hashed}},
                              "dex": {"enabled": False}, "notifications": {"enabled": False}}
    if public:
        values["server"] = {"service": {"type": "LoadBalancer"}}
    return yaml.safe_dump(values)


MONITORING_CHART = "92.3.0"  # kube-prometheus-stack, pinned (helm search repo prometheus-community/kube-prometheus-stack)


def monitoring_values(public: bool = False) -> str:
    """Prometheus + Grafana (kube-prometheus-stack), sized for a small cluster.
    Grafana's admin login comes from the Secret grafana-admin (the install's one password)."""
    return yaml.safe_dump({
        "fullnameOverride": "monitoring",
        "alertmanager": {"enabled": False},
        "grafana": {
            "fullnameOverride": "monitoring-grafana",
            "admin": {"existingSecret": "grafana-admin", "userKey": "admin-user", "passwordKey": "admin-password"},
            "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"memory": "384Mi"}},
            # on EKS: its own AWS load balancer, like Jenkins and Argo CD
            **({"service": {"type": "LoadBalancer"}} if public else {}),
        },
        "prometheus": {"prometheusSpec": {
            "retention": "3d",
            "resources": {"requests": {"cpu": "100m", "memory": "400Mi"}, "limits": {"memory": "1Gi"}},
            # pick up ServiceMonitors from every namespace, not only this release's
            "serviceMonitorSelectorNilUsesHelmValues": False,
            "podMonitorSelectorNilUsesHelmValues": False}},
    }, sort_keys=False)


def argo_app(name: str, repo: str, branch: str, path: str, lmstudio: bool = False) -> str:
    """repo: owner/name of the installer repository; branch: env/<env>."""
    app: dict[str, Any] = {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": name, "namespace": "argocd"},
        "spec": {
            "project": "default",
            "source": {"repoURL": f"https://github.com/{repo}.git", "targetRevision": branch, "path": path},
            "destination": {"server": "https://kubernetes.default.svc", "namespace": "ai-scan-repos"},
            "syncPolicy": {"automated": {"prune": True, "selfHeal": True},
                           "syncOptions": ["CreateNamespace=false"]},
        }}
    if lmstudio:
        # The Tailscale operator rewrites this field; without this Argo CD would fight it.
        app["spec"]["ignoreDifferences"] = [{"kind": "Service", "name": "lmstudio",
                                            "jsonPointers": ["/spec/externalName"]}]
    return yaml.safe_dump(app, sort_keys=False)


def ingress_nginx_values(eks: bool, acm_cert: str = "") -> str:
    svc: dict[str, Any] = {"type": "LoadBalancer"}
    if eks:
        ann = {"service.beta.kubernetes.io/aws-load-balancer-type": "nlb"}
        if acm_cert:
            ann.update({"service.beta.kubernetes.io/aws-load-balancer-ssl-cert": acm_cert,
                        "service.beta.kubernetes.io/aws-load-balancer-ssl-ports": "443",
                        "service.beta.kubernetes.io/aws-load-balancer-backend-protocol": "tcp"})
            svc["targetPorts"] = {"http": "http", "https": "http"}
        svc["annotations"] = ann
    return yaml.safe_dump({"controller": {"service": svc,
                                          "ingressClassResource": {"default": False}}})


def gp3_storageclass() -> str:
    return """apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: ebs.csi.aws.com
volumeBindingMode: WaitForFirstConsumer
reclaimPolicy: Delete
parameters:
  type: gp3
"""


def dump(obj: Any) -> str:
    return json.dumps(obj)
