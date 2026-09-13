#!/bin/sh
# Copy hunt-list snapshots from ~/git/agent-skills into vendor/agent-hints/.
# POSIX sh; no extra deps. Does not overwrite ORIGIN.md.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
DEST="$ROOT/vendor/agent-hints"
SRC_ROOT="${AGENT_SKILLS_ROOT:-$HOME/git/agent-skills}"

PLAN_SRC="$SRC_ROOT/knowledge/plan-skepticism/README.md"
CODE_SRC="$SRC_ROOT/knowledge/code-review-skepticism/README.md"

if [ ! -f "$PLAN_SRC" ] || [ ! -f "$CODE_SRC" ]; then
	echo "refresh-vendor: missing knowledge READMEs under $SRC_ROOT" >&2
	echo "Clone ~/git/agent-skills or set AGENT_SKILLS_ROOT." >&2
	exit 1
fi

mkdir -p "$DEST"

write_snapshot() {
	src=$1
	dest=$2
	origin_knowledge=$3
	origin_cursor=$4
	{
		printf '%s\n' "> Origin: \`${origin_knowledge}\`. Cursor skill: \`${origin_cursor}\`. Refresh: \`sh scripts/refresh-vendor.sh\`."
		printf '%s\n' ""
		cat "$src"
	} >"$dest"
}

write_snapshot "$PLAN_SRC" "$DEST/plan-skepticism.md" \
	"~/git/agent-skills/knowledge/plan-skepticism/README.md" \
	"~/.cursor/skills/skeptic-plan-review/SKILL.md"

write_snapshot "$CODE_SRC" "$DEST/code-review-skepticism.md" \
	"~/git/agent-skills/knowledge/code-review-skepticism/README.md" \
	"~/.cursor/skills/skeptic-code-review/SKILL.md"

echo "Refreshed $DEST/plan-skepticism.md and $DEST/code-review-skepticism.md from $SRC_ROOT"
