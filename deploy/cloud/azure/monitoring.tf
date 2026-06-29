resource "azurerm_monitor_action_group" "main" {
  name                = "${local.name}-alerts"
  resource_group_name = azurerm_resource_group.main.name
  short_name          = "nexus"

  dynamic "email_receiver" {
    for_each = var.alarm_email != "" ? [var.alarm_email] : []
    content {
      name          = "ops"
      email_address = email_receiver.value
    }
  }
}

# Alert when the app has restarting/failed replicas (a proxy for crashes / bad deploys).
resource "azurerm_monitor_metric_alert" "app_replicas" {
  name                = "${local.name}-app-restarts"
  resource_group_name = azurerm_resource_group.main.name
  scopes              = [azurerm_container_app.app.id]
  description         = "App container restart count elevated."
  severity            = 2
  frequency           = "PT1M"
  window_size         = "PT5M"

  criteria {
    metric_namespace = "Microsoft.App/containerApps"
    metric_name      = "RestartCount"
    aggregation      = "Total"
    operator         = "GreaterThan"
    threshold        = 3
  }

  action {
    action_group_id = azurerm_monitor_action_group.main.id
  }
}
