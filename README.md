# Quarc Cleaning

A self-hosted household chore tracker — persons, sections (with parent/child
grouping), cleaning/absence logs, and three analytics views: a per-person
weekly time table (colour-coded green/orange/red), per-section "last cleaned
/ overdue" status, and per-person cleaning counts.

Extracted from Kaan's "skynet" monorepo (originally called "Clean House",
one of several apps that shared a single backend there) into its own
standalone app. The business logic — Tuesday-based week numbering, the
"parent is clean once every primary child is clean" rule, the bottleneck
rule for a parent's own "last cleaned" date — is unchanged from the
original; only the hosting was rebuilt to fit how the rest of Quarc is
deployed.

**Intentionally open, no login** — unlike every other Quarc app, this one
does not require your shared Quarc account. Anyone reachable on the
Tailscale network can open it directly, same as the original design.
"Persons" inside the app are just freeform names you add in Settings, not
tied to any account.

---

## Server Setup

### Requirements

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose
- A machine that stays on, running [Tailscale](https://tailscale.com)
- Tailscale HTTPS Certificates enabled in the admin console

### Step 1 — Clone the repository

```bash
git clone https://github.com/zekicandemiralay/Quarc_Cleaning.git
cd Quarc_Cleaning
```

### Step 2 — Start the server

```bash
bash deploy.sh
```

Nothing to configure first — there's no secret, no API key, and no shared
login to wire up for this app.

### Step 3 — Access the app

```
https://quarcnet0.tail84500c.ts.net:4004
```

No login screen — it opens straight into the app.

### Updating

```bash
git pull
bash deploy.sh
```

The database is stored in a Docker volume (`cleaning_data`) and survives
rebuilds.

---

## Ports

| App | Frontend | Backend |
|---|---|---|
| Quarc Music | 4000 | 3001 |
| quarc-auth | — | 3002 |
| Quarc Notes | 4001 | 3003 |
| Quarc Weather | 4002 | 3004 |
| Quarc Translate | 4003 | 3005 |
| **Quarc Cleaning** | **4004** | **3006** |

---

## Starting fresh

There's no seed data — the three tables (`house_persons`, `house_sections`,
`house_logs`) start empty. Add your own household members and sections from
the **Settings** tab inside the app; everything else (logging a clean,
the time table, the cleanness panel, section counts) works off whatever you
add there.

---

## If you want the shared Quarc login later

This would mean every other Quarc app's approach: gate the whole page behind
your Quarc account (via `quarc-auth`), while leaving the "persons" list
inside exactly as it is now — login would just control who can open the app
at all, not which person a log is attributed to. That's a real change (new
JWT-verification code in the backend, a login screen in the frontend, and
joining the shared `quarcnet-shared` Docker network), not something to
toggle on later without doing that work — ask for it if you change your
mind.
