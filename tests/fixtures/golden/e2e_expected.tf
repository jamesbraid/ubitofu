resource "unifi_network" "examplenet" {
  name    = "examplenet"
  enabled = true
  dhcp_server = {
    enabled = true,
    start   = "10.0.0.10",
  }
}

resource "unifi_device" "switch1" {
  name = "switch1"
  mac  = "aa:bb:cc:00:00:09"

  port_override {
    port_idx = 1
    name     = "uplink"
  }


  port_override {
    port_idx = 2
  }
}
