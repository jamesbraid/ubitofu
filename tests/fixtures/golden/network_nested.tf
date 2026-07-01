resource "unifi_network" "examplenet" {
  name    = "examplenet"
  enabled = true
  dhcp_server = {
    enabled = true,
    start   = "10.0.0.10",
  }
}
