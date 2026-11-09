# Saudi server updater

Install this only after all configured Banner pages pass the server-side `curl` access test. The unattended job runs on the Saudi server, not on the local Mac. It uses a dedicated `kfu` account with a GitHub deploy key restricted to `GoodMan6-dylan/kfu-course` and **Allow write access**. The key for logging in to the server is separate.

## Server setup

1. Install Git, Python 3, OpenSSH client, `flock` (`util-linux`), and `curl` on Ubuntu. The Banner updater uses only Python's standard library. Create a dedicated `kfu` user with home `/home/kfu`, and make it the owner of `/srv/kfu-course`.
2. Generate an Ed25519 key **on the server** at `/home/kfu/.ssh/kfu_course_deploy` with mode `0600`; put only its public key in the repository's **Settings → Deploy keys** with write access. Never put a personal GitHub token or the private deploy key in the repository.
3. Add GitHub's verified SSH host key to `/home/kfu/.ssh/known_hosts` (mode `0600`). Compare the host key fingerprint with [GitHub's published fingerprints](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints) before accepting it.
4. Clone `git@github.com:GoodMan6-dylan/kfu-course.git` to `/srv/kfu-course` as `kfu`, checkout `main`, and set repository-local Git author identity for the automated commits. Keep the worktree clean.
5. Copy `ops/kfu-course-update.service` and `ops/kfu-course-update.timer` to `/etc/systemd/system/`. Then run:

   ```sh
   sudo systemctl daemon-reload
   sudo systemctl enable --now kfu-course-update.timer
   sudo systemctl start kfu-course-update.service
   ```

The first manual `systemctl start` is a one-time smoke test after the access test and deploy key setup. The timer then runs at minute 00, 20, and 40 of each hour. `Persistent=true` starts a missed run after the server comes back online.

## Operations

```sh
sudo systemctl status kfu-course-update.service --no-pager
sudo systemctl list-timers kfu-course-update.timer --no-pager
sudo journalctl -u kfu-course-update.service -n 100 --no-pager
sudo journalctl -t kfu-course-update -p crit --no-pager
```

The wrapper uses `flock` to prevent overlapping runs. It fetches `main`, fast-forwards when the server is behind, or rebases its local unpushed commits if both sides advanced. A rebase conflict is aborted, logged, and stops the run; the wrapper never force-pushes. It then calls `python3 scripts/update_banner.py --repo-root /srv/kfu-course`. The updater's exit codes are `0` for a full fetch, `2` for a partial fetch, and `3` for a safety abort. The wrapper commits `data.js` only if changed, and retries any commit left unpushed by a prior failed push. A partial Banner failure may still publish changes from valid pages while keeping rows from failed pages; a safety abort publishes nothing. Every failed run increments a persistent count. From the second consecutive failure onward, the journal records an `ALERT` at critical priority, including the stage and count. The count resets after a fully successful fetch and push.

Each safe completed check also publishes `check-status.json`, even when course data is unchanged. Its timestamp and result are separate from `data.js`'s `meta.status_updated`, which records the last actual data change. Both files carry `term_code`.

## Changing semesters

Change `TERM_CODE` at the top of `scripts/update_banner.py`. The current term is `144820` (second semester 1448); sources are Engineering, Science, Computer Science and Information Technology, and Language Center for men and women. Verify all eight new URLs from the Saudi server before publishing. The parser validates the semester in the page heading. A term transition requires every page to pass, retains the 30% drop checks, and aborts without writing if any page fails, so old and new semester sections cannot be mixed.

The interface derives term labels and saved timetable keys from `meta.term_code`. Old timetables remain stored under their old term key; CRNs are never carried into a different semester. For a coordinated release, generate and review the new data on the Saudi server and publish it with the updated script and interface.

## Computer Science coverage and legacy migration

The eight full sources cover Engineering (`22`), Science (`08`), Computer Science and Information Technology (`09`), and the Language Center (`00`), for men and women. Computer Science uses `cs_m` / `cs_f`, college codes `09` / `09F`, and department codes derived from the validated Banner department heading and course prefix. No course whitelist or designated-college filter is applied to these pages. Empty instructor names are valid.

Engineering Programming (`0901-204`) now belongs to its actual Banner college, Computer Science, with departments `0901` / `09F01`. An explicit compatibility migration recognizes only the former `eng-programming_m` / `eng-programming_f` source markers for this course. It preserves each CRN, status, instructor, and meeting while correcting the college and department label and removing the obsolete source markers. The original data remains the comparison baseline, so this correction is recorded as an actual change.

Migration occurs before page ownership, retention, and drop counts are evaluated. If a Computer Science fetch fails during the first expanded update, its existing Engineering Programming sections remain under Computer Science; an Engineering-page failure cannot duplicate them. After the first full fetch, all Computer Science sections receive the same protections as every other college: failed or structurally empty pages retain their previous data, a per-page drop greater than 30% is rejected, and a total drop greater than 30% aborts the update without writing. The initial Computer Science baseline contains only the previously imported service-course sections; the reviewed first full fetch establishes the full baseline. All eight sources use the existing 20-minute timer and heartbeat.
