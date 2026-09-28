# Cutover runbook: old Campaigns and Cadences to the engagement engine (Release B)

Spec §13 and §15. This release removes the old engines' code: after it deploys, nothing advances an
old cadence enrollment or sends an old campaign. Run the migration **straight after the deploy**, so
the gap in which in-flight sequences wait is minutes, not days. Nothing is lost in the gap: moved
enrollments keep their due times, and anything overdue goes out on the next tick, in order.

## Before the deploy

1. **Mailbox apps configured in this environment** ([oauth-environments.md](oauth-environments.md)):
   Public base URL, client ids, and the two secrets in Provider keys.
2. **SDRs connect their mailboxes** on My mailboxes (live since Release A). A campaign whose owner has
   no connected mailbox is moved **paused**, with a banner, and nothing of it sends until one is
   chosen (D14).
3. **Backup**: take the database backup (`scripts/backup_db.sh`) and tag the running release.
4. **Heads**: `alembic heads` on the release commit returns exactly one head
   (`0058_engagement_crm_log`). Another branch has planned a `0057_web_cache`: whichever merges
   second renumbers before merging.

## Deploy

1. Deploy the release. `bootstrap_db.py` runs `alembic upgrade head`, then `apply_rls.py` enrols the
   new tables. The engagement engine is **on by default** from this release
   (`engagement_campaigns_enabled`); the old Campaigns and Cadences pages now forward to the new ones.
2. **Dry run, and read it with the owner.** Inside the app container:

   ```bash
   python scripts/migrate_engagement.py --dry-run
   ```

   Per workspace it prints: sequence templates to create, sequences to move and how many will pause
   for lack of a mailbox, opening emails moving to review, old emails found and not found in Sent
   folders (not found means that person's next follow-up starts a new thread), call tasks linked,
   and finished campaigns kept read-only. It writes nothing: it is the real run, rolled back.
   `--json` prints the same for a spreadsheet; `--tenant <id>` limits it to one workspace.
3. **Run it:**

   ```bash
   python scripts/migrate_engagement.py
   ```

   One transaction per workspace: a workspace that fails is reported on stderr, left untouched, and
   the rest still move. The exit code is non-zero if any failed.
4. **Verify the counts** against the dry run, then open Campaigns as an SDR whose sequence moved:
   the campaign is there, its people are at the same step, and **Earlier campaigns** lists the
   finished ones read-only.

## After

- **Re-run the script whenever SDRs connect mailboxes** in the following days. It is idempotent:
  it moves nothing twice, attaches the owner's newly connected mailbox to a campaign that had none,
  resumes its people, and imports the old emails' history (thread recovery from the Sent folder)
  that had nowhere to live the first time. The **Send from this mailbox** control on a paused
  campaign attaches a mailbox too, but only the script imports history.
- Old tables (`campaigns`, `campaign_targets`, `cadences`, `cadence_steps`, `cadence_enrollments`,
  `cadence_touches`) stay as read-only history. Nothing writes them.
- Queued `run_campaign` / `advance_cadences` jobs left over from the old release are logged as
  unknown and dropped, not retried.

## Rollback

Code: redeploy the previous tag. The old engine resumes where it stopped for anything the migration
did not touch; moved enrollments exist in both engines, so **also switch
`engagement_campaigns_enabled` off** (Control plane → Runtime settings) to stop the new engine sending
the same people. Data: nothing was deleted and the migration only added rows, so the backup is for
disaster, not for rollback.
