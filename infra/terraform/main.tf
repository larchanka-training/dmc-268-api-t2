locals {
  bootstrap_script_sha256 = filesha256("${path.module}/scripts/bootstrap.sh")
}

resource "terraform_data" "staging_bootstrap" {
  triggers_replace = [
    var.ssh_host,
    var.ssh_port,
    var.ssh_host_key,
    var.deploy_path,
    local.bootstrap_script_sha256,
  ]

  connection {
    type     = "ssh"
    host     = var.ssh_host
    port     = var.ssh_port
    user     = var.ssh_user
    password = var.ssh_password
    host_key = var.ssh_host_key
    timeout  = "30s"
  }

  provisioner "file" {
    source      = "${path.module}/scripts/bootstrap.sh"
    destination = "/tmp/dmc-268-t2-bootstrap.sh"
  }

  provisioner "remote-exec" {
    inline = [
      "chmod 700 /tmp/dmc-268-t2-bootstrap.sh",
      "/tmp/dmc-268-t2-bootstrap.sh '${var.deploy_path}'",
      "rm -f /tmp/dmc-268-t2-bootstrap.sh",
    ]
  }
}
