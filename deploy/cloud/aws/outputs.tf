output "app_url" {
  value       = "https://${var.domain}"
  description = "Public URL once DNS + ACM propagate."
}

output "ecr_repository_url" {
  value       = aws_ecr_repository.app.repository_url
  description = "Push the app image here (deploy.sh does this)."
}

output "ecs_cluster" {
  value = aws_ecs_cluster.main.name
}

output "alb_dns_name" {
  value = aws_lb.app.dns_name
}

output "log_groups" {
  value = { for k, lg in aws_cloudwatch_log_group.svc : k => lg.name }
}
