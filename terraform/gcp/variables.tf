variable "project_id" {
  description = "GCP project ID to deploy into."
  type        = string
}

variable "region" {
  description = "GCP region."
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "GCP zone."
  type        = string
  default     = "us-central1-a"
}

variable "name" {
  description = "Name prefix for benchmark resources."
  type        = string
  default     = "redis-dsp-bench"
}

variable "machine_type" {
  description = "GCE machine type for the benchmark VM."
  type        = string
  default     = "n2-standard-8"
}

variable "boot_disk_size_gb" {
  description = "Boot disk size in GB."
  type        = number
  default     = 50
}

variable "boot_disk_type" {
  description = "Persistent disk type."
  type        = string
  default     = "pd-ssd"
}

variable "image_family" {
  description = "OS image family."
  type        = string
  default     = "debian-12"
}

variable "image_project" {
  description = "OS image project."
  type        = string
  default     = "debian-cloud"
}

variable "ssh_user" {
  description = "Linux username to create for SSH access."
  type        = string
  default     = "redisbench"
}

variable "ssh_public_key" {
  description = "SSH public key content for the benchmark user."
  type        = string
}

variable "allowed_ssh_cidrs" {
  description = "CIDR blocks allowed to SSH to the VM."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "allowed_app_cidrs" {
  description = "CIDR blocks allowed to access the app on port 8000."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "network" {
  description = "VPC network name."
  type        = string
  default     = "default"
}

variable "subnetwork" {
  description = "Optional subnetwork self link or name. Leave null to use the default subnet in the zone region."
  type        = string
  default     = null
}

variable "service_account_email" {
  description = "Optional service account email for the VM. Leave null to use the Compute Engine default."
  type        = string
  default     = null
}

variable "service_account_scopes" {
  description = "OAuth scopes for the VM service account."
  type        = list(string)
  default = [
    "https://www.googleapis.com/auth/cloud-platform",
  ]
}
