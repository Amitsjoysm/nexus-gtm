locals {
  app_image = var.image != "" ? var.image : var.bootstrap_image

  # Connection URLs come from connection.tf (Terraform-owned, host-aware). POSTGRES_PASSWORD only
  # goes to the self-hosted postgres container; NEXUS_APP_DB_PASSWORD stays in the shared set (the
  # app's entrypoint runs apply_rls.py, which creates the nexus_app role from it).
  data_only = ["POSTGRES_PASSWORD"]

  shared_secrets = [
    for k in keys(var.secrets) : { name = k, valueFrom = aws_ssm_parameter.secret[k].arn }
    if !contains(local.data_only, k)
  ]
  app_db_secrets = [
    { name = "NEXUS_DATABASE_URL", valueFrom = aws_ssm_parameter.conn["NEXUS_DATABASE_URL"].arn },
    { name = "NEXUS_DB_OWNER_URL", valueFrom = aws_ssm_parameter.conn["NEXUS_DB_OWNER_URL"].arn },
  ]
  worker_db_secrets = [
    { name = "NEXUS_DATABASE_URL", valueFrom = aws_ssm_parameter.conn["NEXUS_WORKER_DATABASE_URL"].arn },
  ]
  # Self-hosted data services exist only when not using managed RDS/ElastiCache.
  self_hosted_count = var.use_managed_data ? 0 : 1

  common_environment = [for k, v in var.common_env : { name = k, value = v }]

  log_opts = {
    "awslogs-region"        = var.region
    "awslogs-stream-prefix" = "ecs"
  }
}

resource "aws_ecs_cluster" "main" {
  name = var.project
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

# ============================ POSTGRES (self-hosted, EFS) ============================
resource "aws_ecs_task_definition" "postgres" {
  count                    = local.self_hosted_count
  family                   = "${var.project}-postgres"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.postgres_cpu
  memory                   = var.postgres_memory
  execution_role_arn       = aws_iam_role.execution.arn

  volume {
    name = "pgdata"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.data.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.pgdata.id
        iam             = "DISABLED"
      }
    }
  }

  container_definitions = jsonencode([{
    name         = "postgres"
    image        = "postgres:16-alpine"
    essential    = true
    portMappings = [{ containerPort = 5432 }]
    environment = [
      { name = "POSTGRES_DB", value = "nexus" },
      { name = "POSTGRES_USER", value = "nexus" },
      { name = "PGDATA", value = "/var/lib/postgresql/data/pgdata" },
    ]
    secrets      = [{ name = "POSTGRES_PASSWORD", valueFrom = aws_ssm_parameter.secret["POSTGRES_PASSWORD"].arn }]
    mountPoints  = [{ sourceVolume = "pgdata", containerPath = "/var/lib/postgresql/data" }]
    healthCheck = {
      command     = ["CMD-SHELL", "pg_isready -U nexus -d nexus"]
      interval    = 10
      timeout     = 5
      retries     = 12
      startPeriod = 30
    }
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.log_opts, { "awslogs-group" = aws_cloudwatch_log_group.svc["postgres"].name })
    }
  }])
}

resource "aws_ecs_service" "postgres" {
  count           = local.self_hosted_count
  name            = "${var.project}-postgres"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.postgres[0].arn
  desired_count   = 1
  launch_type     = "FARGATE"
  # A stateful single instance: never run two at once over the same volume.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.data.id]
  }
  service_registries {
    registry_arn = aws_service_discovery_service.postgres.arn
  }
}

# ============================ VALKEY (self-hosted, EFS) ============================
resource "aws_ecs_task_definition" "valkey" {
  count                    = local.self_hosted_count
  family                   = "${var.project}-valkey"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.valkey_cpu
  memory                   = var.valkey_memory
  execution_role_arn       = aws_iam_role.execution.arn

  volume {
    name = "valkey"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.data.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.valkey.id
        iam             = "DISABLED"
      }
    }
  }

  container_definitions = jsonencode([{
    name         = "valkey"
    image        = "valkey/valkey:8-alpine"
    essential    = true
    command      = ["valkey-server", "--appendonly", "yes"]
    portMappings = [{ containerPort = 6379 }]
    mountPoints  = [{ sourceVolume = "valkey", containerPath = "/data" }]
    healthCheck = {
      command     = ["CMD-SHELL", "valkey-cli ping | grep -q PONG"]
      interval    = 10
      timeout     = 5
      retries     = 12
      startPeriod = 10
    }
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.log_opts, { "awslogs-group" = aws_cloudwatch_log_group.svc["valkey"].name })
    }
  }])
}

resource "aws_ecs_service" "valkey" {
  count           = local.self_hosted_count
  name            = "${var.project}-valkey"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.valkey[0].arn
  desired_count   = 1
  launch_type     = "FARGATE"
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.data.id]
  }
  service_registries {
    registry_arn = aws_service_discovery_service.valkey.arn
  }
}

# ============================ APP (API + SPA) ============================
resource "aws_ecs_task_definition" "app" {
  family                   = "${var.project}-app"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.app_cpu
  memory                   = var.app_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name      = "app"
    image     = local.app_image
    essential = true
    # entrypoint.sh runs migrations (NEXUS_RUN_MIGRATIONS=1) then execs this command.
    command      = ["uvicorn", "nexus.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]
    portMappings = [{ containerPort = 8000 }]
    environment  = concat(local.common_environment, [{ name = "NEXUS_RUN_MIGRATIONS", value = "1" }])
    secrets      = concat(local.shared_secrets, local.app_db_secrets)
    healthCheck = {
      command     = ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health',timeout=3).status==200 else 1)\""]
      interval    = 15
      timeout     = 5
      retries     = 5
      startPeriod = 60
    }
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.log_opts, { "awslogs-group" = aws_cloudwatch_log_group.svc["app"].name })
    }
  }])
}

resource "aws_ecs_service" "app" {
  name            = "${var.project}-app"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.app_desired
  launch_type     = "FARGATE"
  # Zero-downtime rolling deploys.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 90

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.app.id]
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "app"
    container_port   = 8000
  }
  depends_on = [aws_lb_listener.https]

  lifecycle {
    ignore_changes = [desired_count] # let autoscaling own it after first apply
  }
}

# ============================ WORKER (queue + heartbeat) ============================
resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.project}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name        = "worker"
    image       = local.app_image
    essential   = true
    command     = ["python", "-m", "nexus.workers.worker"]
    environment = local.common_environment
    secrets     = concat(local.shared_secrets, local.worker_db_secrets)
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.log_opts, { "awslogs-group" = aws_cloudwatch_log_group.svc["worker"].name })
    }
  }])
}

resource "aws_ecs_service" "worker" {
  name            = "${var.project}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  deployment_minimum_healthy_percent = 0 # single consumer; brief gap on deploy is fine (idempotent)
  deployment_maximum_percent         = 200

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.app.id]
  }
}
