provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

locals {
  labels = {
    app  = "redis-dsp-demo"
    role = "benchmark"
  }

  service_account_email = coalesce(var.service_account_email, "default")

  startup_script = templatefile("${path.module}/startup.sh.tftpl", {
    ssh_user = var.ssh_user
  })
}

resource "google_compute_address" "benchmark" {
  name   = "${var.name}-ip"
  region = var.region
}

resource "google_compute_firewall" "ssh" {
  name    = "${var.name}-ssh"
  network = var.network

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  source_ranges = var.allowed_ssh_cidrs
  target_tags   = [var.name]
}

resource "google_compute_firewall" "app" {
  name    = "${var.name}-app"
  network = var.network

  allow {
    protocol = "tcp"
    ports    = ["8000"]
  }

  source_ranges = var.allowed_app_cidrs
  target_tags   = [var.name]
}

resource "google_compute_instance" "benchmark" {
  name         = var.name
  machine_type = var.machine_type
  zone         = var.zone
  tags         = [var.name]
  labels       = local.labels

  boot_disk {
    auto_delete = true
    initialize_params {
      image = "projects/${var.image_project}/global/images/family/${var.image_family}"
      size  = var.boot_disk_size_gb
      type  = var.boot_disk_type
    }
  }

  network_interface {
    network    = var.network
    subnetwork = var.subnetwork
    access_config {
      nat_ip = google_compute_address.benchmark.address
    }
  }

  metadata = {
    ssh-keys = "${var.ssh_user}:${trimspace(var.ssh_public_key)}"
  }

  metadata_startup_script = local.startup_script

  scheduling {
    automatic_restart   = false
    provisioning_model  = "STANDARD"
    preemptible         = false
    on_host_maintenance = "MIGRATE"
  }

  service_account {
    email  = local.service_account_email
    scopes = var.service_account_scopes
  }
}
