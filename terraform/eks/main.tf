# AWS for AI-Scan-Repos: a VPC, an EKS cluster, EFS for the shared reports
# volume, ECR for the four images, an IAM role Jenkins uses to push to ECR,
# and (only with a domain) an ACM certificate for HTTPS.
#
# Run by the installer (terraform init / apply / destroy); state lives on the
# installer's Docker volume. Everything is named after var.name.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.95"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "ai-scan-repos", ManagedBy = "ai-scanner-installer", Install = var.name }
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_caller_identity" "me" {}

locals {
  azs    = slice(data.aws_availability_zones.available.names, 0, 2)
  images = ["review-api", "scanner-worker", "review-gateway", "review-frontend"]
}

# --- network -----------------------------------------------------------------

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.21"

  name = var.name
  cidr = "10.42.0.0/16"
  azs  = local.azs

  private_subnets = ["10.42.1.0/24", "10.42.2.0/24"]
  public_subnets  = ["10.42.101.0/24", "10.42.102.0/24"]

  enable_nat_gateway = true
  single_nat_gateway = true # one NAT for the whole VPC: cheaper, fine for a course project

  public_subnet_tags  = { "kubernetes.io/role/elb" = 1 }
  private_subnet_tags = { "kubernetes.io/role/internal-elb" = 1 }
}

# --- the cluster ---------------------------------------------------------------

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.37"

  cluster_name    = var.name
  cluster_version = var.kubernetes_version

  cluster_endpoint_public_access           = true
  enable_cluster_creator_admin_permissions = true # whoever runs the installer can use kubectl

  # more IAM users/roles with kubectl admin (e.g. your own laptop's AWS identity)
  access_entries = {
    for i, arn in var.admin_arns : "admin-${i}" => {
      principal_arn = arn
      policy_associations = {
        admin = {
          policy_arn   = "arn:aws:eks::aws:policy/AmazonEKSClusterAdminPolicy"
          access_scope = { type = "cluster" }
        }
      }
    }
  }
  enable_irsa                              = true

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  cluster_addons = {
    coredns    = {}
    kube-proxy = {}
    vpc-cni    = {}
    aws-ebs-csi-driver = {
      service_account_role_arn = module.ebs_csi_irsa.iam_role_arn
    }
    aws-efs-csi-driver = {
      service_account_role_arn = module.efs_csi_irsa.iam_role_arn
    }
  }

  eks_managed_node_groups = {
    default = {
      instance_types = [var.node_type]
      min_size       = 1
      max_size       = var.node_count + 1
      desired_size   = var.node_count
      disk_size      = 50 # images, BuildKit cache, scanner work dirs
    }
  }
}

module "ebs_csi_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.55"

  role_name             = "${var.name}-ebs-csi"
  attach_ebs_csi_policy = true
  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["kube-system:ebs-csi-controller-sa"]
    }
  }
}

module "efs_csi_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.55"

  role_name             = "${var.name}-efs-csi"
  attach_efs_csi_policy = true
  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["kube-system:efs-csi-controller-sa", "kube-system:efs-csi-node-sa"]
    }
  }
}

# --- EFS: the reports volume, shared by review-api and every scanner Job -------
# (ReadWriteMany: they can run on different nodes)

resource "aws_efs_file_system" "reports" {
  creation_token = "${var.name}-reports"
  encrypted      = true
  tags           = { Name = "${var.name}-reports" }
}

resource "aws_security_group" "efs" {
  name_prefix = "${var.name}-efs-"
  vpc_id      = module.vpc.vpc_id
  ingress {
    description = "NFS from inside the VPC"
    from_port   = 2049
    to_port     = 2049
    protocol    = "tcp"
    cidr_blocks = [module.vpc.vpc_cidr_block]
  }
}

resource "aws_efs_mount_target" "reports" {
  count           = length(module.vpc.private_subnets)
  file_system_id  = aws_efs_file_system.reports.id
  subnet_id       = module.vpc.private_subnets[count.index]
  security_groups = [aws_security_group.efs.id]
}

# --- ECR: one private repository per image --------------------------------------

resource "aws_ecr_repository" "images" {
  for_each             = toset(local.images)
  name                 = each.key
  image_tag_mutability = "MUTABLE"
  force_delete         = true # `destroy` removes the images too
  image_scanning_configuration {
    scan_on_push = true
  }
}

# --- Jenkins may push to ECR (IRSA on the jenkins/default service account) --------
# The build Pods (ci/pod.yaml) run as that service account.

module "jenkins_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.55"

  role_name = "${var.name}-jenkins-ecr"
  role_policy_arns = {
    ecr = aws_iam_policy.ecr_push.arn
  }
  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["jenkins:default"]
    }
  }
}

resource "aws_iam_policy" "ecr_push" {
  name = "${var.name}-ecr-push"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
      {
        Effect = "Allow"
        Action = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload",
          "ecr:GetDownloadUrlForLayer", "ecr:InitiateLayerUpload", "ecr:PutImage", "ecr:UploadLayerPart"]
        Resource = [for r in aws_ecr_repository.images : r.arn]
      },
    ]
  })
}

# --- HTTPS (only when a domain is given) ---------------------------------------
# DNS validation: the installer shows the CNAME to add at the domain's DNS host.

resource "aws_acm_certificate" "app" {
  count             = var.domain == "" ? 0 : 1
  domain_name       = var.domain
  validation_method = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}
