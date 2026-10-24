#!/bin/bash
# Shared helpers for the golden harness (sourced, not executed).

GOLDEN_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
GOLDEN_REPO=$(cd -- "$GOLDEN_DIR/../.." &>/dev/null && pwd)

# golden-load-scenario NAME : export _base.env then scenarios/NAME.env
function golden-load-scenario () {
	local name=$1
	if [[ ! -f "$GOLDEN_DIR/scenarios/$name.env" ]]
	then
		printf "Unknown scenario: %s\n" "$name" >&2
		return 1
	fi
	set -a
	# shellcheck disable=SC1090
	source "$GOLDEN_DIR/scenarios/_base.env"
	# shellcheck disable=SC1090
	source "$GOLDEN_DIR/scenarios/$name.env"
	set +a
}

# golden-list-scenarios : one scenario name per line (no leading underscore files)
function golden-list-scenarios () {
	local f
	for f in "$GOLDEN_DIR"/scenarios/[!_]*.env
	do
		basename "$f" .env
	done
}

# golden-offline-env LOGFILE : fixed locale + fake curl/sleep first on PATH
function golden-offline-env () {
	export LC_ALL=C.utf8
	export LANG=C.utf8
	export GOLDEN_NET_LOG=$1
	: > "$GOLDEN_NET_LOG"
	export PATH="$GOLDEN_DIR/fakebin:$PATH"
}
