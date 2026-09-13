variable "ssh_host" {
  description = "Existing staging VPS IP address or hostname."
  type        = string
}

variable "ssh_port" {
  description = "SSH port of the staging VPS."
  type        = number
  default     = 22
}

variable "ssh_user" {
  description = "SSH user used for provisioning."
  type        = string
  sensitive   = true
  ephemeral   = true
}

variable "ssh_password" {
  description = "SSH password used for provisioning."
  type        = string
  sensitive   = true
  ephemeral   = true
}

variable "ssh_host_key" {
  description = "Pinned SSH host public key."
  type        = string
}

variable "deploy_path" {
  description = "Directory on the VPS reserved for the Team 2 staging deployment."
  type        = string
  default     = "/opt/dmc-268-t2"

  validation {
    condition     = can(regex("^/[A-Za-z0-9._/-]+$", var.deploy_path))
    error_message = "deploy_path must be an absolute path containing only safe path characters."
  }
}
