# Where the data is

- `db.query` runs read-only SQL over the product database:
  - `users` (`id`, `email`, `full_name`, `created_at`)
  - `subscriptions` (`user_id`, `plan`, `paid_at`, `amount`, `active_until`, `auto_renew`)
  - `credit_grants` (`user_id`, `amount`, `added_at`, `expires_at`, `source`)
  - `credit_spends` (`user_id`, `amount`, `spent_at`, `grant_id`, `reason`)
  - `payments` (`id`, `user_id`, `amount`, `status`, `paid_at`): payments for plans and credit packs
  - `generations` (`id`, `user_id`, `model`, `status`, `error`, `prompt`, `created_at`, `credits`, `refunded`)
- `errors.search` searches the error tracker of the web app and the services.
- `code.search` and `code.read` read the code of the product.
- `tracker.search` searches the task tracker of the developers.
