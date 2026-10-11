variable "name" {
  description = "Name for the cluster and every resource (letters, digits, dashes)"
  type        = string
  default     = "ai-scan-repos"
}

variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "kubernetes_version" {
  description = "EKS Kubernetes version"
  type        = string
  default     = "1.33"
}

variable "node_type" {
  description = "EC2 instance type of the worker nodes"
  type        = string
  default     = "t3.large"
}

variable "node_count" {
  description = "How many worker nodes"
  type        = number
  default     = 2
}

variable "domain" {
  description = "Optional: a domain (e.g. scan.example.com) to serve over HTTPS with an ACM certificate"
  type        = string
  default     = ""
}

variable "admin_arns" {
  description = "Extra IAM user/role ARNs that get cluster-admin through kubectl"
  type        = list(string)
  default     = []
}
