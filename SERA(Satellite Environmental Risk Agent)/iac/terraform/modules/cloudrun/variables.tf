variable "project_id"          { type = string }
variable "region"              { type = string }
variable "env"                 { type = string }
variable "service_name"        { type = string }
variable "image"               { type = string }
variable "api_sa_email"        { type = string }
variable "ingest_sa_email"     { type = string }
variable "cloud_sql_conn_name" { type = string }
variable "bq_dataset"          { type = string }
variable "gcs_bucket"          { type = string }
variable "redis_secret_name"   { type = string }
variable "db_url_secret_name"  { type = string }

