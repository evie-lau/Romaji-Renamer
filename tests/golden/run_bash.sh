#!/bin/bash
# Reference implementation of the golden "write metadata" stage: runs ONLY the final write loop of
# animes-renamer.sh / movies-renamer.sh against a fixture, using the real functions.sh.
#
# Usage: run_bash.sh --script-folder DIR --media animes|movies --output FILE
#    or: run_bash.sh --scenario NAME --output FILE [--script-folder DIR]   (standalone convenience)
# All settings are read from the process environment (see scenarios/_base.env).

SELF_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
source "$SELF_DIR/lib.sh"

SCRIPT_FOLDER=""
media_type=""
OUT=""
SCENARIO=""
while [[ $# -gt 0 ]]
do
	case "$1" in
		--script-folder) SCRIPT_FOLDER=$2; shift 2 ;;
		--media) media_type=$2; shift 2 ;;
		--output) OUT=$2; shift 2 ;;
		--scenario) SCENARIO=$2; shift 2 ;;
		*) printf "Unknown argument: %s\n" "$1" >&2; exit 2 ;;
	esac
done

if [[ -n "$SCENARIO" ]]
then
	golden-load-scenario "$SCENARIO" || exit 2
	[[ -n "$media_type" ]] || media_type=$GOLDEN_MEDIA
	if [[ -z "$SCRIPT_FOLDER" ]]
	then
		SCRIPT_FOLDER="$SELF_DIR/.work/standalone-$SCENARIO"
		python3 "$SELF_DIR/make_fixture.py" --out "$SCRIPT_FOLDER" || exit 2
	fi
	golden-offline-env "$SCRIPT_FOLDER/config/net-attempts.log"
fi
if [[ -z "$SCRIPT_FOLDER" || -z "$media_type" || -z "$OUT" ]]
then
	printf "Usage: %s --script-folder DIR --media animes|movies --output FILE\n" "$0" >&2
	exit 2
fi
SCRIPT_FOLDER=$(cd -- "$SCRIPT_FOLDER" &>/dev/null && pwd) || exit 2
[[ "$media_type" == animes || "$media_type" == movies ]] || { printf "Bad --media\n" >&2; exit 2; }
case "$OUT" in /*) ;; *) OUT="$PWD/$OUT" ;; esac

: "${LOG_FOLDER:=$SCRIPT_FOLDER/config/logs}"
: "${ASSET_FOLDER:=$SCRIPT_FOLDER/pmm/assets}"
mkdir -p "$LOG_FOLDER"
source "$SCRIPT_FOLDER/VERSION"
source "$GOLDEN_REPO/functions.sh"
METADATA=$OUT
OVERRIDE=override-ID-$media_type.tsv

:> "$MATCH_LOG"

# ---- below: copied from the write stage of animes-renamer.sh / movies-renamer.sh ----
printf "%s - Start writing the metadata file \n" "$(date +%H:%M:%S)" | tee -a "$LOG"
printf "# Romaji-Renamer v%s\n" "$version" > "$METADATA"
printf "metadata:\n" >> "$METADATA"
if [[ $media_type == "animes" ]]
then
	tvdb_id=""
	anilist_id=""
	mal_id=""
	while IFS=$'\t' read -r tvdb_id anilist_id plex_title asset_name seasons_list override_seasons_ignore
	do
		printf "%s\t - Writing metadata for %s / tvdb : %s / Anilist : %s \n" "$(date +%H:%M:%S)" "$plex_title" "$tvdb_id" "$anilist_id" | tee -a "$LOG"
		write-metadata
		printf "%s\t - Done\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
	done < "$SCRIPT_FOLDER/config/ID/animes.tsv"
else
	imdb_id=""
	anilist_id=""
	mal_id=""
	while IFS=$'\t' read -r imdb_id anilist_id plex_title asset_name
	do
		printf "%s\t - Writing metadata for %s / imdb : %s / Anilist : %s \n" "$(date +%H:%M:%S)" "$plex_title" "$imdb_id" "$anilist_id" | tee -a "$LOG"
		write-metadata
		printf "%s\t - Done\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
	done < "$SCRIPT_FOLDER/config/ID/movies.tsv"
fi
printf "%s - Run finished\n\n\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
exit 0
