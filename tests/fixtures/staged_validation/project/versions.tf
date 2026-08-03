terraform {
  required_version = ">= 1.10"

  required_providers {
    random = {
      source  = "hashicorp/random"
      version = "3.7.2"
    }
    http = {
      source  = "hashicorp/http"
      version = "3.5.0"
    }
  }

  backend "http" {
    address = "http://127.0.0.1:1/backend-trap"
  }
}

resource "random_id" "proof" {
  byte_length = 1
}

data "http" "provider_api_trap" {
  url = "http://127.0.0.1:1/provider-api-trap"
}

module "registry" {
  source  = "cloudposse/label/null"
  version = "0.25.0"

  namespace = "stage"
  name      = "proof"
}

module "inside" {
  source = "./modules/inside"
  proof  = "inside"
}

module "outside" {
  source = "../outside"
  proof  = "outside"
}
