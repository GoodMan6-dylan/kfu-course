#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="${KFU_REPO_ROOT:-/srv/kfu-course}"
state_dir="${KFU_STATE_DIR:-/var/lib/kfu-course-updater}"
failure_file="$state_dir/consecutive_failures"

mkdir -p "$state_dir"
exec 9>"$state_dir/run.lock"
if ! flock -n 9; then
  printf '%s\n' 'INFO: KFU update is already running; skipping this timer event.'
  exit 0
fi

failures=0
failure_recorded=0
if [[ -f "$failure_file" ]]; then
  read -r failures < "$failure_file" || failures=0
  [[ "$failures" =~ ^[0-9]+$ ]] || failures=0
fi

record_failure() {
  local stage="$1"
  if (( failure_recorded == 0 )); then
    failures=$((failures + 1))
    printf '%s\n' "$failures" > "$failure_file.tmp"
    mv -f "$failure_file.tmp" "$failure_file"
    failure_recorded=1
  fi
  printf 'ERROR: KFU update failed at %s; consecutive failures: %s.\n' "$stage" "$failures" >&2
  if (( failures >= 2 )); then
    local alert="ALERT: KFU course update failed ${failures} consecutive times at ${stage}; inspect journalctl -u kfu-course-update.service."
    printf '%s\n' "$alert" >&2
    logger -t kfu-course-update -p user.crit -- "$alert" || true
  fi
}

record_success() {
  if (( failures > 0 )); then
    printf 'INFO: KFU update recovered after %s consecutive failures.\n' "$failures"
  fi
  printf '0\n' > "$failure_file.tmp"
  mv -f "$failure_file.tmp" "$failure_file"
}

cd "$repo_root"
export GIT_SSH_COMMAND='ssh -i /home/kfu/.ssh/kfu_course_deploy -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o BatchMode=yes'

if [[ "$(git branch --show-current)" != main ]]; then
  record_failure 'git branch check'
  exit 1
fi

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  record_failure 'dirty worktree check'
  exit 1
fi

if ! git fetch origin main:refs/remotes/origin/main; then
  record_failure 'GitHub fetch'
  exit 1
fi

if git merge-base --is-ancestor origin/main HEAD; then
  printf '%s\n' 'INFO: local main already contains origin/main.'
elif git merge-base --is-ancestor HEAD origin/main; then
  if ! git merge --ff-only origin/main; then
    record_failure 'GitHub fast-forward'
    exit 1
  fi
else
  if ! git merge-base HEAD origin/main >/dev/null; then
    record_failure 'GitHub history check (no common ancestor)'
    exit 1
  fi
  printf '%s\n' 'INFO: remote main advanced while local commits were unpushed; rebasing local commits.'
  if ! GIT_EDITOR=true GIT_SEQUENCE_EDITOR=true git rebase origin/main; then
    if ! git rebase --abort; then
      printf '%s\n' 'ERROR: rebase abort failed; repository requires manual inspection.' >&2
    fi
    record_failure 'GitHub rebase (conflict or error)'
    exit 1
  fi
fi

set +e
python3 scripts/update_banner.py --repo-root "$repo_root"
updater_status=$?
set -e

case "$updater_status" in
  0)
    ;;
  2)
    record_failure 'partial Banner fetch'
    ;;
  3)
    record_failure 'Banner safety abort'
    if ! git diff --quiet -- data.js check-status.json; then
      printf '%s\n' 'ERROR: updater changed a published file during a safety abort; leaving it untouched for inspection.' >&2
    fi
    exit 3
    ;;
  *)
    record_failure "Banner updater (exit $updater_status)"
    exit "$updater_status"
    ;;
esac

git add -- data.js
if [[ -f check-status.json ]]; then
  git add -- check-status.json
fi
if ! git diff --cached --quiet -- data.js check-status.json; then
  if git diff --cached --quiet -- data.js; then
    commit_message='Record KFU Banner check'
  else
    commit_message='Update KFU Banner sections'
  fi
  if ! git commit -m "$commit_message"; then
    record_failure 'Git commit'
    exit 1
  fi
  printf 'INFO: committed %s.\n' "$commit_message"
else
  printf '%s\n' 'INFO: Banner data and check status are unchanged; no commit created.'
fi

if [[ "$(git rev-list --count origin/main..HEAD)" != 0 ]]; then
  if ! git push origin HEAD:main; then
    record_failure 'GitHub push'
    exit 1
  fi
  printf '%s\n' 'INFO: published local commits to main.'
fi

if (( updater_status == 0 )); then
  record_success
else
  exit "$updater_status"
fi
