variable "name_prefix" {
  description = "Prefix for every resource name."
  type        = string
  default     = "docintel"
}

variable "location" {
  description = "Azure region. Document Intelligence is not in every region; eastus, westus2, westeurope are safe."
  type        = string
  default     = "eastus"
}

variable "docintel_sku" {
  description = "F0 is free (500 pages/month, one per subscription) - enough for the 30 sample documents. S0 is pay-as-you-go, ~USD 1.50 per 1,000 pages."
  type        = string
  default     = "F0"
  validation {
    condition     = contains(["F0", "S0"], var.docintel_sku)
    error_message = "docintel_sku must be F0 or S0."
  }
}

variable "tags" {
  type    = map(string)
  default = { project = "docintel", managed_by = "terraform" }
}
