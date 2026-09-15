# Everything document ingestion writes to. Two containers and one OCR service.
#
#   raw       the uploaded PDFs
#   curated   Document Intelligence output: documents/<doc_id>.txt + .json
#
# Authentication is Azure AD only - no storage keys, no API keys. The identity
# that runs `terraform apply` is granted the two data-plane roles the scripts
# need, so `az login` is the whole setup.

data "azurerm_client_config" "current" {}

resource "random_string" "suffix" {
  length  = 6
  upper   = false
  special = false
}

resource "azurerm_resource_group" "this" {
  name     = "${var.name_prefix}-ingest-rg"
  location = var.location
  tags     = var.tags
}

# ---------------------------------------------------------------- storage ---
resource "azurerm_storage_account" "this" {
  name                     = "${var.name_prefix}ingest${random_string.suffix.result}"
  resource_group_name      = azurerm_resource_group.this.name
  location                 = azurerm_resource_group.this.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  min_tls_version          = "TLS1_2"

  # AAD-only data plane: the scripts use DefaultAzureCredential, never a key.
  shared_access_key_enabled       = false
  allow_nested_items_to_be_public = false
  https_traffic_only_enabled      = true

  blob_properties {
    versioning_enabled = true
    delete_retention_policy {
      days = 7
    }
  }

  tags = var.tags
}

resource "azurerm_storage_container" "raw" {
  name                  = "raw"
  storage_account_id    = azurerm_storage_account.this.id
  container_access_type = "private"
}

resource "azurerm_storage_container" "curated" {
  name                  = "curated"
  storage_account_id    = azurerm_storage_account.this.id
  container_access_type = "private"
}

# ------------------------------------------------- document intelligence ---
resource "azurerm_cognitive_account" "docintel" {
  name                  = "${var.name_prefix}-docintel-${random_string.suffix.result}"
  resource_group_name   = azurerm_resource_group.this.name
  location              = azurerm_resource_group.this.location
  kind                  = "FormRecognizer"
  sku_name              = var.docintel_sku
  custom_subdomain_name = "${var.name_prefix}-docintel-${random_string.suffix.result}"

  # AAD auth only. `prebuilt-read` needs no key when the caller holds the
  # Cognitive Services User role.
  local_auth_enabled = false

  tags = var.tags
}

# ------------------------------------------------------------- data roles ---
# The deploying identity gets exactly what the scripts need and nothing more.
# Granting these requires Owner or User Access Administrator on the subscription.

resource "azurerm_role_assignment" "blob_contributor" {
  scope                = azurerm_storage_account.this.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_role_assignment" "docintel_user" {
  scope                = azurerm_cognitive_account.docintel.id
  role_definition_name = "Cognitive Services User"
  principal_id         = data.azurerm_client_config.current.object_id
}
