# Core LAN — hand maintained. Comments and layout MUST survive reconcile.
resource "unifi_network" "core_lan" {
  name    = "Core LAN renamed"
  vlan    = 11   # keep this pinned; changed via change control only
  mtu     = 1500 # jumbo frames deliberately off
  enabled = false

  # Nested block: reconcile must NOT touch these (flag-only).
  dhcp_server = {
    enabled = true
    start   = "10.0.10.100"
  }
}

resource "unifi_network" "guest" {
  name = "Guest"
  vlan = 20 # isolated
}
