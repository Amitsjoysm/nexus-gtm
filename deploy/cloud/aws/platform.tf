# ---- Container registry ----
resource "aws_ecr_repository" "app" {
  name                 = var.project
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
  force_delete = true
}

# ---- Service discovery (private DNS): postgres.<ns>, valkey.<ns> ----
resource "aws_service_discovery_private_dns_namespace" "internal" {
  name        = "${var.project}.local"
  vpc         = aws_vpc.main.id
  description = "Internal DNS for self-hosted data services."
}

resource "aws_service_discovery_service" "postgres" {
  name = "postgres"
  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.internal.id
    dns_records {
      ttl  = 10
      type = "A"
    }
    routing_policy = "MULTIVALUE"
  }
  health_check_custom_config { failure_threshold = 1 }
}

resource "aws_service_discovery_service" "valkey" {
  name = "valkey"
  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.internal.id
    dns_records {
      ttl  = 10
      type = "A"
    }
    routing_policy = "MULTIVALUE"
  }
  health_check_custom_config { failure_threshold = 1 }
}

# ---- Persistent storage (EFS) for postgres + valkey ----
resource "aws_efs_file_system" "data" {
  creation_token = "${var.project}-data"
  encrypted      = true
  tags           = { Name = "${var.project}-data" }
}

resource "aws_efs_mount_target" "data" {
  count           = 2
  file_system_id  = aws_efs_file_system.data.id
  subnet_id       = aws_subnet.private[count.index].id
  security_groups = [aws_security_group.efs.id]
}

# POSIX ownership must match the container's runtime uid (adjust if you change image versions).
resource "aws_efs_access_point" "pgdata" {
  file_system_id = aws_efs_file_system.data.id
  posix_user {
    uid = 70 # postgres user in postgres:16-alpine
    gid = 70
  }
  root_directory {
    path = "/pgdata"
    creation_info {
      owner_uid   = 70
      owner_gid   = 70
      permissions = "0700"
    }
  }
}

resource "aws_efs_access_point" "valkey" {
  file_system_id = aws_efs_file_system.data.id
  posix_user {
    uid = 999 # valkey user in valkey:8-alpine
    gid = 1000
  }
  root_directory {
    path = "/valkey"
    creation_info {
      owner_uid   = 999
      owner_gid   = 1000
      permissions = "0755"
    }
  }
}

# ---- Secrets in SSM Parameter Store (SecureString) ----
resource "aws_ssm_parameter" "secret" {
  for_each = var.secrets
  name     = "/${var.project}/${var.env}/${each.key}"
  type     = "SecureString"
  value    = each.value
  tags     = { Name = each.key }
}

# ---- CloudWatch log groups (one per service) ----
resource "aws_cloudwatch_log_group" "svc" {
  for_each          = toset(["app", "worker", "postgres", "valkey"])
  name              = "/ecs/${var.project}/${each.key}"
  retention_in_days = var.log_retention_days
}

# ---- IAM ----
data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Execution role: pull images, write logs, read the SSM secrets.
resource "aws_iam_role" "execution" {
  name               = "${var.project}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  name = "read-ssm-secrets"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameters"]
      Resource = "arn:aws:ssm:${var.region}:*:parameter/${var.project}/${var.env}/*"
    }]
  })
}

# Task role: what the running app may call (e.g. S3 for nightly pg_dump backups).
resource "aws_iam_role" "task" {
  name               = "${var.project}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}
