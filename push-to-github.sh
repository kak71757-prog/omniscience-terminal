#!/usr/bin/env bash
# Export this repo to GitHub as `omniscience-terminal`.
#
#   ./push-to-github.sh git@github.com:<you>/omniscience-terminal.git
#   ./push-to-github.sh https://<TOKEN>@github.com/<you>/omniscience-terminal.git
#
# The repository must already exist on GitHub (create it empty — no README,
# no .gitignore — so the first push is not rejected).
set -euo pipefail
cd "$(dirname "$0")"

REMOTE="${1:-}"
if [ -z "${REMOTE}" ]; then
  echo "usage: $0 <github-repo-url>"
  exit 1
fi

git init -q 2>/dev/null || true
git config user.email "${GIT_EMAIL:-engineer@omniscience.local}"
git config user.name  "${GIT_NAME:-Omniscience}"

if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "${REMOTE}"
else
  git remote add origin "${REMOTE}"
fi

git add -A
git diff --cached --quiet || git commit -q -m "PROJECT OMNISCIENCE — Railway deployment"

git branch -M main
echo "pushing to ${REMOTE%%@*}…" | sed 's#//[^@]*@#//***@#'
git push -u origin main
echo "done. deploy it: Railway → New Project → Deploy from GitHub → omniscience-terminal"
