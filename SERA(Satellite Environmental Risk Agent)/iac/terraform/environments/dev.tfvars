# SERA — dev environment config
# project_id is NOT here — set it at runtime:
#   export TF_VAR_project_id=$(gcloud config get-value project)
#
# cloud_run_sa_email is also not here — main.tf computes it from the project number

region = "asia-south1"
env    = "dev"

cloud_run_service_name  = "sera"
cloud_sql_instance_name = "sera-postgres"
cloud_sql_db_name       = "sera"
cloud_sql_user          = "sera"

bq_dataset = "sera_analytics_dev"
gcs_bucket = "sera-rasters-dev"

artifact_registry_repo = "sera"
git_branch             = "develop"

# Actual Secret Manager names (differ from deploy.sh defaults)
redis_secret_name  = "sera-redis-srt"
db_url_secret_name = "sera-db-url"

