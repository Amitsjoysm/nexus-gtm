# VARIANT (a): managed data tier — created only when use_managed_data = true. When enabled, the
# self-hosted postgres/valkey ECS services in services.tf are skipped (count = 0) and the
# connection URLs in connection.tf point here instead. EFS resources stay but go unused (negligible
# cost) so the toggle is a single variable with no other file edits.

# ============================ RDS PostgreSQL (Multi-AZ) ============================
resource "aws_db_subnet_group" "main" {
  count      = var.use_managed_data ? 1 : 0
  name       = "${var.project}-db"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_security_group" "rds" {
  count       = var.use_managed_data ? 1 : 0
  name_prefix = "${var.project}-rds-"
  vpc_id      = aws_vpc.main.id
  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  lifecycle { create_before_destroy = true }
  tags = { Name = "${var.project}-rds-sg" }
}

resource "aws_db_instance" "main" {
  count                        = var.use_managed_data ? 1 : 0
  identifier                   = "${var.project}-pg"
  engine                       = "postgres"
  engine_version               = "16"
  instance_class               = var.rds_instance_class
  allocated_storage            = var.rds_allocated_storage
  max_allocated_storage        = var.rds_max_allocated_storage # storage autoscaling
  storage_type                 = "gp3"
  storage_encrypted            = true
  db_name                      = "nexus"
  username                     = "nexus"
  password                     = local.pg_password
  multi_az                     = var.rds_multi_az
  db_subnet_group_name         = aws_db_subnet_group.main[0].name
  vpc_security_group_ids       = [aws_security_group.rds[0].id]
  backup_retention_period      = var.rds_backup_retention_days
  backup_window                = "03:00-04:00"
  maintenance_window           = "sun:04:30-sun:05:30"
  deletion_protection          = var.rds_deletion_protection
  skip_final_snapshot          = false
  final_snapshot_identifier    = "${var.project}-pg-final"
  performance_insights_enabled = true
  auto_minor_version_upgrade   = true
  apply_immediately            = false
  tags                         = { Name = "${var.project}-pg" }
}

# ============================ ElastiCache (Redis, HA) ============================
resource "aws_elasticache_subnet_group" "main" {
  count      = var.use_managed_data ? 1 : 0
  name       = "${var.project}-redis"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_security_group" "redis" {
  count       = var.use_managed_data ? 1 : 0
  name_prefix = "${var.project}-redis-"
  vpc_id      = aws_vpc.main.id
  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
  lifecycle { create_before_destroy = true }
  tags = { Name = "${var.project}-redis-sg" }
}

resource "aws_elasticache_replication_group" "main" {
  count                      = var.use_managed_data ? 1 : 0
  replication_group_id       = "${var.project}-redis"
  description                = "Infojoy GTM queue + cache"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = var.redis_node_type
  num_cache_clusters         = 1 + var.redis_replicas      # 1 primary + N replicas
  automatic_failover_enabled = var.redis_replicas >= 1
  multi_az_enabled           = var.redis_replicas >= 1
  port                       = 6379
  subnet_group_name          = aws_elasticache_subnet_group.main[0].name
  security_group_ids         = [aws_security_group.redis[0].id]
  at_rest_encryption_enabled = true
  # In-VPC only, so plain redis:// is used (TLS would require rediss:// + an AUTH token).
  snapshot_retention_limit = 7
  apply_immediately        = false
  tags                     = { Name = "${var.project}-redis" }
}
