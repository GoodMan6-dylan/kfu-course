# Saudi server updater

Install this only after all six Banner pages pass the server-side `curl` access test. The unattended job runs on the Saudi server, not on the local Mac. It uses a dedicated `kfu` account with a GitHub deploy key restricted to `GoodMan6-dylan/kfu-course` and **Allow write access**. The key for logging in to the server is separate.

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
