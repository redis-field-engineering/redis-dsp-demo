output "instance_name" {
  value = google_compute_instance.benchmark.name
}

output "zone" {
  value = google_compute_instance.benchmark.zone
}

output "public_ip" {
  value = google_compute_address.benchmark.address
}

output "ssh_command" {
  value = "ssh ${var.ssh_user}@${google_compute_address.benchmark.address}"
}

output "app_url" {
  value = "http://${google_compute_address.benchmark.address}:8000"
}

