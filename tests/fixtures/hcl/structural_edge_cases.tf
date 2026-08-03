# Café before every structural span exercises UTF-8 byte offsets.
variable "region" {
  type    = string
  default = "Zürich"
}

locals {
  duplicate    = "resource \"unifi_network\" \"decoy\" {}"
  interpolated = "hello ${var.region}"
}

resource "unifi_network" "edge" {
  name = "réseau-${var.region}"

  description = <<-EOT
    resource "unifi_network" "heredoc_decoy" {
      name = "not a block"
    }
    ${var.region}
  EOT

  lifecycle {
    ignore_changes = [description]
  }

  tags = {
    nested = {
      reference = var.region
    }
  }
}

# resource "unifi_network" "comment_decoy" {}
import {
  to = unifi_network.edge
  id = "65f00d"
}
