output "cluster_name" {
  value = module.eks.cluster_name
}

output "region" {
  value = var.region
}

output "ecr_registry" {
  description = "The registry prefix Jenkins pushes to (REGISTRY)"
  value       = "${data.aws_caller_identity.me.account_id}.dkr.ecr.${var.region}.amazonaws.com"
}

output "efs_id" {
  value = aws_efs_file_system.reports.id
}

output "jenkins_role_arn" {
  value = module.jenkins_irsa.iam_role_arn
}

output "acm_certificate_arn" {
  value = var.domain == "" ? "" : aws_acm_certificate.app[0].arn
}

output "acm_validation" {
  description = "The DNS record to add so ACM can issue the certificate"
  value = var.domain == "" ? [] : [for o in aws_acm_certificate.app[0].domain_validation_options : {
    name  = o.resource_record_name
    type  = o.resource_record_type
    value = o.resource_record_value
  }]
}
