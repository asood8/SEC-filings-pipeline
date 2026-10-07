terraform {
  required_version = ">= 1.10.0, < 2.0.0"

  required_providers {
    mongodbatlas = {
      source  = "mongodb/mongodbatlas"
      version = ">= 2.19, < 3.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.9, < 4.0"
    }
  }
}
