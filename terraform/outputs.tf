output "resource_group" {
  value = azurerm_resource_group.this.name
}

output "storage_account" {
  description = "Export as AZURE_STORAGE_ACCOUNT before running the scripts."
  value       = azurerm_storage_account.this.name
}

output "docintel_endpoint" {
  description = "Export as AZURE_DOCINTEL_ENDPOINT before running the scripts."
  value       = azurerm_cognitive_account.docintel.endpoint
}

output "containers" {
  value = [azurerm_storage_container.raw.name, azurerm_storage_container.curated.name]
}
