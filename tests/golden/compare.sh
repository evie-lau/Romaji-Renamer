#!/bin/bash
# Run every scenario with a given implementation and diff against expected/<scenario>.yml
#
# Usage: compare.sh [-u] [-w] [-j N] [-s NAME]... [-k] IMPL-COMMAND [args...]
#   IMPL-COMMAND is invoked as:  IMPL-COMMAND --script-folder DIR --media animes|movies --output FILE
#   with the scenario settings exported in its environment.
#   -w  warm check: run the implementation a 2nd time on the same fixture (fragment cache populated);
#       the 2nd output must be identical too and its log must report 'Fragment cache: N hits' with N > 0
#   -u  update expected/ from this implementation (use ONLY with the bash reference run_bash.sh)
#   -j  parallel jobs (default 4)     -s  run only this scenario (repeatable)     -k  keep work dirs
# Example: tests/golden/compare.sh tests/golden/run_bash.sh
#          tests/golden/compare.sh python3 tools/write_metadata.py
# Exit status: 0 all identical, 1 any diff / offline violation / implementation failure.

SELF_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
source "$SELF_DIR/lib.sh"

UPDATE=0
WARM=0
JOBS=4
KEEP=0
ONLY=()
while getopts "uwj:s:k" opt
do
	case "$opt" in
		u) UPDATE=1 ;;
		w) WARM=1 ;;
		j) JOBS=$OPTARG ;;
		s) ONLY+=("$OPTARG") ;;
		k) KEEP=1 ;;
		*) exit 2 ;;
	esac
done
shift $((OPTIND - 1))
[[ $# -gt 0 ]] || { sed -n '2,12p' "$0"; exit 2; }
IMPL=("$@")
case "${IMPL[0]}" in
	*/*) [[ "${IMPL[0]}" == /* ]] || IMPL[0]="$PWD/${IMPL[0]}" ;;
esac

if [[ ${#ONLY[@]} -gt 0 ]]
then
	SCENARIOS=("${ONLY[@]}")
else
	mapfile -t SCENARIOS < <(golden-list-scenarios)
fi
WORK="$SELF_DIR/.work/run-$$"
mkdir -p "$WORK" "$SELF_DIR/expected"

function run-one () {
	local name=$1
	local dir="$WORK/$name"
	local rc=0
	mkdir -p "$dir"
	(
		golden-load-scenario "$name" || exit 2
		golden-offline-env "$dir/net-attempts.log"
		python3 "$SELF_DIR/make_fixture.py" --out "$dir/fixture" --username "$ANILIST_USERNAME" || exit 2
		cd "$dir" || exit 2
		"${IMPL[@]}" --script-folder "$dir/fixture" --media "$GOLDEN_MEDIA" --output "$dir/actual.yml" > "$dir/stdout.log" 2> "$dir/stderr.log"
		rc=$?
		if [[ $rc -eq 0 && $WARM == 1 && $UPDATE == 0 ]]
		then
			"${IMPL[@]}" --script-folder "$dir/fixture" --media "$GOLDEN_MEDIA" --output "$dir/actual-warm.yml" > "$dir/stdout-warm.log" 2> "$dir/stderr-warm.log"
			rc=$?
		fi
		exit $rc
	)
	rc=$?
	if [[ $rc -ne 0 ]]
	then
		printf "FAIL  %-32s implementation exited %s (see %s/stderr.log)\n" "$name" "$rc" "$dir" > "$dir/result"
		return
	fi
	if [[ -s "$dir/net-attempts.log" ]]
	then
		printf "FAIL  %-32s network/sleep attempted: %s\n" "$name" "$(head -n 1 "$dir/net-attempts.log")" > "$dir/result"
		return
	fi
	if [[ $UPDATE == 1 ]]
	then
		cp "$dir/actual.yml" "$SELF_DIR/expected/$name.yml"
		printf "WROTE %s\n" "$name" > "$dir/result"
	elif ! diff -u "$SELF_DIR/expected/$name.yml" "$dir/actual.yml" > "$dir/diff.txt"
	then
		printf "FAIL  %-32s output differs (%s/diff.txt)\n" "$name" "$dir" > "$dir/result"
	elif [[ $WARM == 1 ]] && ! diff -u "$SELF_DIR/expected/$name.yml" "$dir/actual-warm.yml" > "$dir/diff-warm.txt"
	then
		printf "FAIL  %-32s warm-cache output differs (%s/diff-warm.txt)\n" "$name" "$dir" > "$dir/result"
	elif [[ $WARM == 1 ]] && ! grep -Eq 'Fragment cache: [1-9][0-9]* hits' "$dir/stdout-warm.log"
	then
		printf "FAIL  %-32s warm run had no fragment cache hits (%s/stdout-warm.log)\n" "$name" "$dir" > "$dir/result"
	else
		printf "PASS  %s\n" "$name" > "$dir/result"
	fi
}

for name in "${SCENARIOS[@]}"
do
	while [[ $(jobs -rp | wc -l) -ge $JOBS ]]
	do
		wait -n
	done
	run-one "$name" &
done
wait

fail=0
for name in "${SCENARIOS[@]}"
do
	cat "$WORK/$name/result"
	grep -q '^FAIL' "$WORK/$name/result" && fail=1
done
if [[ $KEEP == 0 && $fail == 0 ]]
then
	rm -rf "$WORK"
else
	printf "Work dir kept: %s\n" "$WORK"
fi
exit $fail
