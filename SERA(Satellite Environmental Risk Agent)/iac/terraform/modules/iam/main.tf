resource "google_service_account" "sera" {
  for_each     = local.service_accounts
  project      = var.project_id
  account_id   = each.value.account_id
  display_name = each.value.display_name
}

resource "google_project_iam_member" "sera" {
  for_each = local.role_bindings
  project  = var.project_id
  role     = each.value.role
  member   = "serviceAccount:${google_service_account.sera[each.value.sa_key].email}"
}

