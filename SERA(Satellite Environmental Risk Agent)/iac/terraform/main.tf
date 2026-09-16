terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }

  # GCS backend — all config passed at init time, nothing hardcoded here.
  # Dev:  terraform init -backend-config="bucket=$(gcloud config get-value project)_cloudbuild" -backend-config="prefix=terraform-state/sera/dev"
  # Prod: terraform init -backend-config="bucket=$(gcloud config get-value project)_cloudbuild" -backend-config="prefix=terraform-state/sera/prod"
  # Create the bucket once (see iac/terraform/backend-setup.sh).
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# ── IAM: service accounts + role bindings ────────────────────────────────────

module "iam" {
  source     = "./modules/iam"
  project_id = var.project_id
  env        = var.env
}

# ── Secrets (names + API/ingest SA accessor bindings — no values) ─────────────

module "secrets" {
  source             = "./modules/secrets"
  project_id         = var.project_id
  redis_secret_name  = var.redis_secret_name
  db_url_secret_name = var.db_url_secret_name
  api_sa_email       = module.iam.api_sa_email
  ingest_sa_email    = module.iam.ingest_sa_email
}

# ── Cloud SQL ─────────────────────────────────────────────────────────────────

module "cloudsql" {
  source        = "./modules/cloudsql"
  project_id    = var.project_id
  region        = var.region
  instance_name = var.cloud_sql_instance_name
  db_name       = var.cloud_sql_db_name
  db_user       = var.cloud_sql_user

  depends_on = [module.iam]
}

# ── Storage (GCS + BigQuery) ──────────────────────────────────────────────────

module "storage" {
  source          = "./modules/storage"
  project_id      = var.project_id
  region          = var.region
  env             = var.env
  gcs_bucket      = var.gcs_bucket
  bq_dataset      = var.bq_dataset
  ingest_sa_email = module.iam.ingest_sa_email
}

# ── Artifact Registry ─────────────────────────────────────────────────────────

resource "google_artifact_registry_repository" "sera" {
  project       = var.project_id
  location      = var.region
  repository_id = var.artifact_registry_repo
  format        = "DOCKER"
  description   = "SERA container images"
}

# ── CI/CD triggers: IaC plan/apply + app build ───────────────────────────────

#module "cicd" {
#  source         = "./modules/cicd"
#  project_id     = var.project_id
#  region         = var.region
#  env            = var.env
#  branch         = var.git_branch
#  deploy_bucket  = "${var.project_id}_cloudbuild"
#}

# ── Cloud Run API service ─────────────────────────────────────────────────────

module "cloudrun" {
  source              = "./modules/cloudrun"
  project_id          = var.project_id
  region              = var.region
  env                 = var.env
  service_name        = var.cloud_run_service_name
  image               = "${local.image_base}:latest"
  api_sa_email        = module.iam.api_sa_email
  ingest_sa_email     = module.iam.ingest_sa_email
  cloud_sql_conn_name = local.sql_conn
  bq_dataset          = var.bq_dataset
  gcs_bucket          = var.gcs_bucket
  redis_secret_name   = var.redis_secret_name
  db_url_secret_name  = var.db_url_secret_name

  depends_on = [
    module.iam,
    module.secrets,
    module.cloudsql,
  ]
}


