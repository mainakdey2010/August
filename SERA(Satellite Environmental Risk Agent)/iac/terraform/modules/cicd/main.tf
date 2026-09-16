locals {
  sera_dir = "SERA(Satellite Environmental Risk Agent)"
}

# ── IaC triggers: plan on PR, apply on merge ──────────────────────────────────
# Follows BTDP pattern: two triggers per env — one for review, one for deploy.

resource "google_cloudbuild_trigger" "iac_plan" {
  project  = var.project_id
  name     = "sera-iac-plan-${var.env}"
  location = var.region

  github {
    owner = var.github_owner
    name  = var.github_repo
    pull_request {
      branch = "^${var.branch}$"
    }
  }

  included_files = ["${local.sera_dir}/iac/**"]

  build {
    step {
      name       = "hashicorp/terraform:1.5"
      entrypoint = "sh"
      args = ["-c", <<-EOT
        cd ${local.sera_dir}/iac/terraform &&
        terraform init \
          -backend-config="bucket=${var.deploy_bucket}" \
          -backend-config="prefix=terraform-state/sera/${var.env}" \
          -input=false &&
        terraform plan \
          -var-file=environments/${var.env}.tfvars \
          -var="project_id=$PROJECT_ID" \
          -input=false \
          -out=/dev/null
      EOT
      ]
    }
    options { logging = "CLOUD_LOGGING_ONLY" }
    timeout = "600s"
  }
}

resource "google_cloudbuild_trigger" "iac_apply" {
  project  = var.project_id
  name     = "sera-iac-apply-${var.env}"
  location = var.region

  github {
    owner = var.github_owner
    name  = var.github_repo
    push {
      branch = "^${var.branch}$"
    }
  }

  included_files = ["${local.sera_dir}/iac/**"]

  build {
    step {
      name       = "hashicorp/terraform:1.5"
      entrypoint = "sh"
      args = ["-c", <<-EOT
        cd ${local.sera_dir}/iac/terraform &&
        terraform init \
          -backend-config="bucket=${var.deploy_bucket}" \
          -backend-config="prefix=terraform-state/sera/${var.env}" \
          -input=false &&
        terraform apply \
          -var-file=environments/${var.env}.tfvars \
          -var="project_id=$PROJECT_ID" \
          -input=false \
          -auto-approve
      EOT
      ]
    }
    options { logging = "CLOUD_LOGGING_ONLY" }
    timeout = "900s"
  }
}

# ── App build + push trigger (image only — no deploy until dev milestone done) ─

resource "google_cloudbuild_trigger" "app_build" {
  project  = var.project_id
  name     = "sera-build-${var.env}"
  location = var.region

  github {
    owner = var.github_owner
    name  = var.github_repo
    push {
      branch = "^${var.branch}$"
    }
  }

  # Fires on src/ or Dockerfile changes — not on iac/ changes (separate trigger above)
  included_files = [
    "${local.sera_dir}/src/**",
    "${local.sera_dir}/Dockerfile",
    "${local.sera_dir}/cloudbuild.yaml",
  ]

  filename = "${local.sera_dir}/cloudbuild.yaml"
}

