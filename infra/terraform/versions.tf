terraform {
  required_version = ">= 1.10.0, < 2.0.0"

  cloud {
    organization = "BTTF-Team-2"

    workspaces {
      name = "btte-team-2"
    }
  }
}
