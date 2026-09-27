# ClearPath dummy JSON database

Synthetic reference data that sits beside `pa-engine/` under `data/`.  
It is **not** wired into `pa-engine` (SQLite remains the live engine DB). Use this folder for demo auth and seed records while the product UI grows.

## Auth (id or name only)

No passwords. Look up a user by:

- user `id`
- `login_name` (e.g. `ana.reyes`, `suman`)
- `display_name` (e.g. `Dr. Maya Anderson`)
- provider `npi` / provider name
- patient `member_id` / patient name

```bash
# from repo root
python data/db_client.py login "Dr. Ana Reyes"
python data/db_client.py login ana.reyes
python data/db_client.py login 1679651234
python data/db_client.py login suman
python data/db_client.py login "Maria Rodriguez"
python data/db_client.py snapshot
```

`login` writes a row into `sessions.json` and appends to `audit_log.json`.

## Collections (4–5 demo rows each)

| File | Purpose |
|------|---------|
| `users.json` | Portal auth roles: admin, clinician, insurer, patient |
| `providers.json` | Doctors with `id` + `npi` + specialty |
| `patients.json` | Synthetic patients with member id / MRN |
| `clinics.json` | Ordering clinics (org NPI) |
| `facilities.json` | Imaging / ASC / hospital sites |
| `payers.json` | Insurers |
| `coverage_plans.json` | Plan + year + sample PA services |
| `patient_insurance.json` | Member coverage links |
| `pa_requests.json` | Seed PA cases the app can grow |
| `sessions.json` | Active demo logins (starts empty) |
| `audit_log.json` | Mutation / login trail (starts empty) |

## Updating as the app runs

From Python (outside `pa-engine`):

```python
from data.db_client import login, upsert, load

auth = login("maya.anderson")          # doctor portal
upsert("pa_requests", {                 # append / update a PA row
    "id": "pa-demo-0006",
    "patient_id": "...",
    "status": "draft",
    "order_text": "MRI lumbar spine without contrast",
    "service_code": "72148",
})
```

All patients and providers are marked `synthetic: true`.
