#!/usr/bin/env bash
#
# Syntax-check every Ansible task file — including dynamically included ones.
#
# `ansible-playbook --syntax-check playbook.yml` only parses tasks reachable by
# STATIC means (roles:, import_tasks, import_role). Files pulled in with
# `include_tasks` (dynamic) are never parsed, so a templated shell block in one
# of them with an unbalanced quote or Jinja block — e.g. an apostrophe in a
# comment like `# this task's no_log` — sails past lint and only blows up at
# run time with "failed at splitting arguments". (cron.yml hit exactly this.)
#
# This forces the parser through every task file by STATICALLY importing each
# one into a throwaway playbook and syntax-checking that. Undefined vars are
# fine — --syntax-check parses templates without evaluating them; the quote/
# Jinja imbalance is a parse-level error and still surfaces.
#
# Usage: ./scripts/check-task-syntax.sh   (run from anywhere; needs ansible)

set -euo pipefail

ANSIBLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../ansible" && pwd)"
cd "$ANSIBLE_DIR"

wrapper="$(mktemp -t ansible-task-syntax.XXXXXX.yml)"
trap 'rm -f "$wrapper"' EXIT

{
  echo "- hosts: localhost"
  echo "  gather_facts: false"
  echo "  tasks:"
  count=0
  for f in roles/*/tasks/*.yml; do
    [ -e "$f" ] || continue
    echo "    - import_tasks: ${ANSIBLE_DIR}/${f}"
    count=$((count + 1))
  done
  if [ "$count" -eq 0 ]; then
    echo "ERROR: no task files found under roles/*/tasks/" >&2
    exit 1
  fi
} > "$wrapper"

echo "Syntax-checking $(grep -c import_tasks "$wrapper") task files (incl. dynamically included)..."
ansible-playbook --syntax-check -i localhost, "$wrapper"
echo "OK: all task files parse cleanly."
