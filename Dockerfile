# The Project Installer Image: one container with the wizard and every tool
# a deploy needs (kubectl, helm, terraform, aws cli, git, ssh).
#
#   docker buildx build --platform linux/amd64,linux/arm64 -t <registry>/ai-scanner-installer:<tag> --push .
#
# How to run it: README.md.

# --- the wizard page ----------------------------------------------------------
FROM node:22-alpine AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

# --- the installer ------------------------------------------------------------
FROM python:3.12-slim
ARG TARGETARCH
ARG KUBECTL_VERSION=v1.33.5
ARG HELM_VERSION=v3.19.0
ARG TERRAFORM_VERSION=1.13.3

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git openssh-client unzip \
 && rm -rf /var/lib/apt/lists/*

# kubectl (kustomize is built in: kubectl kustomize / apply -k)
RUN curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
 && chmod +x /usr/local/bin/kubectl

# helm
RUN curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${TARGETARCH}.tar.gz" \
    | tar -xz -C /tmp && mv "/tmp/linux-${TARGETARCH}/helm" /usr/local/bin/helm && rm -rf /tmp/linux-*

# terraform
RUN curl -fsSLo /tmp/tf.zip "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip" \
 && unzip -q /tmp/tf.zip -d /usr/local/bin && rm /tmp/tf.zip

# AWS CLI v2 (its own architectures are named x86_64 / aarch64)
RUN ARCH=$([ "$TARGETARCH" = "arm64" ] && echo aarch64 || echo x86_64) \
 && curl -fsSLo /tmp/aws.zip "https://awscli.amazonaws.com/awscli-exe-linux-${ARCH}.zip" \
 && unzip -q /tmp/aws.zip -d /tmp && /tmp/aws/install && rm -rf /tmp/aws /tmp/aws.zip

WORKDIR /opt/installer
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY installer/ installer/
COPY terraform/ terraform/
COPY --from=ui /ui/dist ui/dist

ENV INSTALLER_DATA=/data \
    INSTALLER_UI=/opt/installer/ui/dist \
    INSTALLER_TERRAFORM=/opt/installer/terraform/eks \
    HOST_KUBECONFIG=/host/kube/config \
    HOST_MINIKUBE=/host/minikube \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8088 8081 8082 8083
CMD ["uvicorn", "installer.app:app", "--host", "0.0.0.0", "--port", "8088"]
