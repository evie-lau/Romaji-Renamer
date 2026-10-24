#!/bin/bash

#General variables
LOG=$LOG_FOLDER/RR_$(date +%Y.%m.%d).log
MATCH_LOG=$LOG_FOLDER/${media_type}-missing-id.log

# functions
function create-override () {
	if [ ! -f "$SCRIPT_FOLDER/config/$OVERRIDE" ]
	then
		cp "$SCRIPT_FOLDER/config/override-ID-${media_type}.example.tsv" "$SCRIPT_FOLDER/config/$OVERRIDE"
	fi
}
function cache-is-fresh () {
	local file=$1
	local max_days=$2
	local modified
	local now

	[[ -s "$file" ]] || return 1
	[[ "$max_days" =~ ^[0-9]+$ ]] || return 1
	modified=$(stat -c %Y -- "$file") || return 1
	now=$(date +%s) || return 1
	(( now - modified < max_days * 86400 ))
}
function cache-time-for-status () {
	local cache_ttl

	case "$1" in
		FINISHED|CANCELLED)
			cache_ttl=${CACHE_TIME_FINISHED:-${DATA_CACHE_TIME:-5}}
			;;
		RELEASING|HIATUS)
			cache_ttl=${CACHE_TIME_AIRING:-${DATA_CACHE_TIME:-5}}
			;;
		NOT_YET_RELEASED)
			cache_ttl=${CACHE_TIME_UPCOMING:-${DATA_CACHE_TIME:-5}}
			;;
		*)
			cache_ttl=${DATA_CACHE_TIME:-5}
			;;
	esac
	if [[ ! "$cache_ttl" =~ ^[0-9]+$ ]]
	then
		printf "Error invalid cache TTL: %s\n" "$cache_ttl" >&2
		return 1
	fi
	printf "%s" "$cache_ttl"
}
function download-static-json () {
	local filename=$1
	local url=$2
	local file="$SCRIPT_FOLDER/config/data/$filename"
	local etag_file="$file.etag"
	local new_file="$file.tmp"
	local new_etag="$etag_file.tmp"
	local cache_valid=0
	local status
	local size
	local wait_time=0
	local -a curl_args

	if [[ -s "$file" ]] && [[ $(du -b "$file" | awk '{ print $1 }') -gt 1000 ]] && jq -e . "$file" >/dev/null 2>&1
	then
		cache_valid=1
	fi
	while [[ $wait_time -lt 5 ]]
	do
		curl_args=(curl -sS --output "$new_file" --write-out "%{http_code}" --etag-save "$new_etag")
		if [[ $cache_valid == 1 ]] && [[ -s "$etag_file" ]]
		then
			curl_args+=(--etag-compare "$etag_file")
		fi
		status=$("${curl_args[@]}" "$url")
		if [[ $? == 0 ]]
		then
			if [[ "$status" == "304" ]] && [[ $cache_valid == 1 ]]
			then
				rm -f "$new_file" "$new_etag"
				printf "%s - Using cached %s\n" "$(date +%H:%M:%S)" "$filename" | tee -a "$LOG"
				return 0
			fi
			size=$(du -b "$new_file" 2>/dev/null | awk '{ print $1 }')
			if [[ "$status" == "200" ]] && [[ "$size" =~ ^[0-9]+$ ]] && [[ "$size" -gt 1000 ]] && jq -e . "$new_file" >/dev/null 2>&1
			then
				if ! mv "$new_file" "$file"
				then
					printf "%s - Error saving downloaded %s\n" "$(date +%H:%M:%S)" "$filename" | tee -a "$LOG"
					return 1
				fi
				if [[ -s "$new_etag" ]]
				then
					mv "$new_etag" "$etag_file"
				else
					rm -f "$etag_file"
				fi
				printf "%s - Downloaded %s\n" "$(date +%H:%M:%S)" "$filename" | tee -a "$LOG"
				return 0
			fi
		fi
		rm -f "$new_file" "$new_etag"
		((wait_time++))
		if [[ $wait_time == 4 ]]
		then
			printf "%s - Error can't download %s stopping script\n" "$(date +%H:%M:%S)" "$filename" | tee -a "$LOG"
			return 1
		fi
		printf "%s - Invalid or failed response for %s, retrying\n" "$(date +%H:%M:%S)" "$filename" | tee -a "$LOG"
		sleep 30
	done
	return 1
}
function create-mapping-indexes () {
	local mapping_file
	local raw_file

	if [[ $media_type == "animes" ]]
	then
		mapping_file="$SCRIPT_FOLDER/config/data/list-animes-id.json"
		raw_file="$SCRIPT_FOLDER/config/tmp/anime-mapping.tsv"
		jq -r '.[] | [(.anilist_id // ""), (.tvdb_id // ""), (.tvdb_season // ""), (.tvdb_epoffset // ""), (.mal_id // "")] | @tsv' "$mapping_file" > "$raw_file"
		awk -F '\t' '($3 == "1" || $3 == "-1") && $4 == "0" && !seen[$2]++ { print $2 "\t" $1 }' "$raw_file" > "$SCRIPT_FOLDER/config/tmp/tvdb-anilist.tsv"
		awk -F '\t' '!seen[$1]++ { print $1 "\t" $5 }' "$raw_file" > "$SCRIPT_FOLDER/config/tmp/anilist-mal.tsv"
		awk -F '\t' '{ print $1 "\t" $2 }' "$raw_file" > "$SCRIPT_FOLDER/config/tmp/anilist-tvdb.tsv"
	else
		mapping_file="$SCRIPT_FOLDER/config/data/list-movies-id.json"
		raw_file="$SCRIPT_FOLDER/config/tmp/movie-mapping.tsv"
		jq -r '.[] | [(.anilist_id // ""), (.imdb_id // ""), (.mal_id // "")] | @tsv' "$mapping_file" > "$raw_file"
		awk -F '\t' '!seen[$2]++ { print $2 "\t" $1 }' "$raw_file" > "$SCRIPT_FOLDER/config/tmp/imdb-anilist.tsv"
		awk -F '\t' '!seen[$1]++ { print $1 "\t" $3 }' "$raw_file" > "$SCRIPT_FOLDER/config/tmp/anilist-mal.tsv"
	fi
}
function tsv-has-value () {
	local file=$1
	local field=$2
	local value=$3

	awk -F '\t' -v key="$value" -v column="$field" '$column == key { found = 1; exit } END { if (!found) exit 1 }' "$file"
}
function tsv-get-field () {
	local file=$1
	local key_field=$2
	local key=$3
	local output_field=$4

	awk -F '\t' -v key="$key" -v key_column="$key_field" -v value_column="$output_field" '$key_column == key { print $value_column; exit }' "$file"
}
function download-anime-id-mapping () {
	if [[ $media_type == "animes" ]]
	then
		download-static-json "list-animes-id.json" "https://raw.githubusercontent.com/Arial-Z/Animes-ID/main/list-animes-id.json" || exit 1
	else
		download-static-json "list-movies-id.json" "https://raw.githubusercontent.com/Arial-Z/Animes-ID/main/list-movies-id.json" || exit 1
	fi
	download-static-json "cr-award.json" "https://raw.githubusercontent.com/Arial-Z/Animes-ID/main/cr-award/cr-award.json" || exit 1
	create-mapping-indexes
}
function get-anilist-userlist {
	if [[ $ANILIST_LISTS == "Yes" ]]
	then
		printf "%s - Creating Anilist userlist for : %s\n" "$(date +%H:%M:%S)" "$ANILIST_USERNAME" | tee -a "$LOG"
		wait_time=0
		anilist_api_retry=0
		while [ $wait_time -lt 5 ] || [ $anilist_api_retry -lt 5 ];
		do
			printf "%s\t - Downloading Anilist userlist\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
			curl -s 'https://graphql.anilist.co/' \
			-X POST \
			-H 'content-type: application/json' \
			--data '{ "query": "{ MediaListCollection(userName: \"'"$ANILIST_USERNAME"'\" type:ANIME) {  lists {    name    entries {      mediaId    }  }}}" }' > "$SCRIPT_FOLDER/config/tmp/anilist-$ANILIST_USERNAME.json" -D "$SCRIPT_FOLDER/config/tmp/anilist-limit-rate.txt"
			if grep -q -w '"data": null' "$SCRIPT_FOLDER/config/tmp/anilist-$ANILIST_USERNAME.json"
			then
				((anilist_api_retry++))
				rm "$SCRIPT_FOLDER/config/tmp/anilist-$ANILIST_USERNAME.json"
				printf "%s - Invalid json from AniList API down, waiting 60s\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
				sleep 61
			fi
			rate_limit=0
			rate_limit=$(grep -oP '(?<=x-ratelimit-remaining: )[0-9]+' "$SCRIPT_FOLDER/config/tmp/anilist-limit-rate.txt")
			((wait_time++))
			if [[ $wait_time == 4 ]] || [[ $anilist_api_retry == 4 ]]
			then
				printf "%s - Error can't download anilist data stopping script\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
				exit 1
			elif [[ -z $rate_limit ]]
			then
				printf "%s\t - Cloudflare limit rate reached watiting 60s\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
				sleep 61
			elif [[ $rate_limit -ge 3 ]]
			then
				sleep 1
				printf "%s\t - Done\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
				break
			elif [[ $rate_limit -lt 3 ]]
			then
				printf "%s\t - Anilist API limit reached watiting 30s" "$(date +%H:%M:%S)" | tee -a "$LOG"
				sleep 30
				printf "%s\t - Done\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
				break
			fi
		done
		printf "%s\t - Sorting Anilist userlist\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
		jq '.data.MediaListCollection.lists | .[] | select( .name == "Completed" ) | .entries | .[].mediaId ' -r "$SCRIPT_FOLDER/config/tmp/anilist-$ANILIST_USERNAME.json" | paste -s -d, - > "$SCRIPT_FOLDER/config/data/anilist-$ANILIST_USERNAME-Completed.tsv"
		jq '.data.MediaListCollection.lists | .[] | select( .name == "Watching" ) | .entries | .[].mediaId ' -r "$SCRIPT_FOLDER/config/tmp/anilist-$ANILIST_USERNAME.json" | paste -s -d, - > "$SCRIPT_FOLDER/config/data/anilist-$ANILIST_USERNAME-Watching.tsv"
		jq '.data.MediaListCollection.lists | .[] | select( .name == "Dropped" ) | .entries | .[].mediaId ' -r "$SCRIPT_FOLDER/config/tmp/anilist-$ANILIST_USERNAME.json" | paste -s -d, - > "$SCRIPT_FOLDER/config/data/anilist-$ANILIST_USERNAME-Dropped.tsv"
		jq '.data.MediaListCollection.lists | .[] | select( .name == "Paused" ) | .entries | .[].mediaId ' -r "$SCRIPT_FOLDER/config/tmp/anilist-$ANILIST_USERNAME.json" | paste -s -d, - > "$SCRIPT_FOLDER/config/data/anilist-$ANILIST_USERNAME-Paused.tsv"
		jq '.data.MediaListCollection.lists | .[] | select( .name == "Planning" ) | .entries | .[].mediaId ' -r "$SCRIPT_FOLDER/config/tmp/anilist-$ANILIST_USERNAME.json" | paste -s -d, - > "$SCRIPT_FOLDER/config/data/anilist-$ANILIST_USERNAME-Planning.tsv"
		printf "%s\t - Done\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
		printf "%s - done\n\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
	fi
}
function get-anilist-id () {
	if [[ $media_type == "animes" ]]
	then
		awk -F '\t' -v key="$tvdb_id" '$1 == key { print $2; exit }' "$SCRIPT_FOLDER/config/tmp/tvdb-anilist.tsv"
	else
		awk -F '\t' -v key="$imdb_id" '$1 == key { print $2; exit }' "$SCRIPT_FOLDER/config/tmp/imdb-anilist.tsv"
	fi
}
function get-mal-id () {
	mal_id=$(jq '.data.Media.idMal' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json")
	if [[ $mal_id == 'null' ]] || [[ -z $mal_id ]]
	then
		mal_id=$(awk -F '\t' -v key="$anilist_id" '$1 == key { print $2; exit }' "$SCRIPT_FOLDER/config/tmp/anilist-mal.tsv")
	fi
}
function get-tvdb-id () {
	awk -F '\t' -v key="$anilist_id" '$1 == key { print $2 }' "$SCRIPT_FOLDER/config/tmp/anilist-tvdb.tsv"
}
function anilist-cache-is-fresh () {
	local file="$SCRIPT_FOLDER/config/data/anilist-$1.json"
	local status
	local ttl

	status=$(jq -er '.data.Media | select(type == "object") | .status | strings | select(length > 0)' "$file" 2>/dev/null) || return 1
	ttl=$(cache-time-for-status "$status") || return 1
	cache-is-fresh "$file" "$ttl"
}
function collect-anilist-ids () {
	if [[ -s "$SCRIPT_FOLDER/config/ID/animes.tsv" ]] && [[ -s "$SCRIPT_FOLDER/config/tmp/anime-mapping.tsv" ]] && [[ "$1" == "animes" ]]
	then
		# main entries plus every mapped cour of the tvdb shows in the library
		awk -F '\t' 'NR == FNR { tvdb[$1]; print $2; next } $2 in tvdb { print $1 }' "$SCRIPT_FOLDER/config/ID/animes.tsv" "$SCRIPT_FOLDER/config/tmp/anime-mapping.tsv"
	elif [[ "$1" == "movies" ]]
	then
		awk -F '\t' '{ print $3 }' "$SCRIPT_FOLDER/config/ID/movies.tsv"
	fi
}
# Fetch one batch of AniList ids and write each result as a per-id cache file
function anilist-fetch-batch () {
	local query='query($ids: [Int], $perPage: Int) { Page(perPage: $perPage) { media(type: ANIME, id_in: $ids) { id relations { edges { relationType node { id type format title { romaji } status } } } title { romaji(stylised: false) english(stylised: false) native(stylised: false) } averageScore genres tags { name rank } studios { edges { node { name isAnimationStudio } } } startDate { year month } season seasonYear coverImage { extraLarge } status idMal } } }'
	local request="$SCRIPT_FOLDER/config/tmp/anilist-batch-request.json"
	local response="$SCRIPT_FOLDER/config/tmp/anilist-batch-response.json"
	local headers="$SCRIPT_FOLDER/config/tmp/anilist-limit-rate.txt"
	local attempt=0
	local status
	local delay
	local remaining
	local id
	local json
	local saved=0

	jq -n --arg query "$query" --argjson ids "$(printf '%s\n' "$@" | jq -Rn '[inputs | tonumber]')" --argjson count "$#" '{query: $query, variables: {ids: $ids, perPage: $count}}' > "$request"
	while [[ $attempt -lt 3 ]]
	do
		((attempt++))
		status=$(curl -sS --connect-timeout 10 --max-time 60 -o "$response" -D "$headers" -w "%{http_code}" 'https://graphql.anilist.co/' -X POST -H 'content-type: application/json' --data @"$request") || status=000
		if [[ "$status" == "200" ]] && jq -e '.data.Page.media | type == "array"' "$response" >/dev/null 2>&1
		then
			break
		fi
		delay=$(grep -ioP '(?<=retry-after: )[0-9]+' "$headers" 2>/dev/null | head -n 1)
		[[ "$status" == "429" ]] || delay=10
		printf "%s\t - AniList batch request failed (HTTP %s), retrying in %ss\n" "$(date +%H:%M:%S)" "$status" "${delay:-60}" | tee -a "$LOG"
		sleep "${delay:-60}"
	done
	if [[ "$status" != "200" ]] || ! jq -e '.data.Page.media | type == "array"' "$response" >/dev/null 2>&1
	then
		rm -f "$request" "$response"
		return 1
	fi
	while IFS=$'\t' read -r id json
	do
		printf '%s\n' "$json" > "$SCRIPT_FOLDER/config/data/anilist-$id.json.tmp"
		if jq -e '.data.Media | type == "object" and (.status | type == "string")' "$SCRIPT_FOLDER/config/data/anilist-$id.json.tmp" >/dev/null 2>&1
		then
			mv "$SCRIPT_FOLDER/config/data/anilist-$id.json.tmp" "$SCRIPT_FOLDER/config/data/anilist-$id.json"
			((saved++))
		else
			rm -f "$SCRIPT_FOLDER/config/data/anilist-$id.json.tmp"
		fi
	done < <(jq -r '.data.Page.media[] | "\(.id)\t\({data: {Media: .}} | tojson)"' "$response")
	printf "%s\t - Cached %s of %s requested ids\n" "$(date +%H:%M:%S)" "$saved" "$#" | tee -a "$LOG"
	remaining=$(grep -ioP '(?<=x-ratelimit-remaining: )[0-9]+' "$headers" 2>/dev/null | head -n 1)
	rm -f "$request" "$response"
	if [[ -n "$remaining" ]] && [[ $remaining -lt 3 ]]
	then
		printf "%s\t - Anilist API limit nearly reached waiting 30s\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
		sleep 30
	fi
	return 0
}
# Warm the AniList cache with batched requests; ids that are missed fall back to get-anilist-infos
function prefetch-anilist-infos () {
	local media_kind="$1"
	local pending="$SCRIPT_FOLDER/config/tmp/anilist-prefetch-ids.txt"
	local batch_size=50
	local id
	local fd
	local pass=0
	local max_passes=50
	local failed=0
	local -a chunk
	local -a frontier
	local -a files
	local -A seen

	printf "%s - Prefetching Anilist data\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
	: > "$pending"
	while read -r id
	do
		[[ "$id" =~ ^[0-9]+$ ]] || continue
		seen[$id]=1
		frontier+=("$id")
		anilist-cache-is-fresh "$id" || printf '%s\n' "$id" >> "$pending"
	done < <(collect-anilist-ids "$media_kind" | sort -nu)
	printf "%s\t - %s ids to download\n" "$(date +%H:%M:%S)" "$(wc -l < "$pending")" | tee -a "$LOG"
	exec {fd}< "$pending"
	while mapfile -t -n "$batch_size" -u "$fd" chunk && [[ ${#chunk[@]} -gt 0 ]]
	do
		if ! anilist-fetch-batch "${chunk[@]}"
		then
			printf "%s\t - Batch failed, remaining ids will be downloaded one by one\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
			failed=1
			break
		fi
	done
	exec {fd}<&-
	# only animes walk sequel chains (get-airing-status), so prefetch those targets too
	while [[ "$media_kind" == "animes" ]] && [[ $failed -eq 0 ]] && [[ $pass -lt $max_passes ]] && [[ ${#frontier[@]} -gt 0 ]]
	do
		((pass++))
		files=()
		for id in "${frontier[@]}"
		do
			[[ -f "$SCRIPT_FOLDER/config/data/anilist-$id.json" ]] && files+=("$SCRIPT_FOLDER/config/data/anilist-$id.json")
		done
		frontier=()
		: > "$pending"
		if [[ ${#files[@]} -gt 0 ]]
		then
			while read -r id
			do
				[[ "$id" =~ ^[0-9]+$ ]] || continue
				[[ -n "${seen[$id]}" ]] && continue
				seen[$id]=1
				frontier+=("$id")
				anilist-cache-is-fresh "$id" || printf '%s\n' "$id" >> "$pending"
			done < <(printf '%s\0' "${files[@]}" | xargs -0 jq -r '.data.Media.relations.edges[]? | select(.relationType == "SEQUEL") | .node | select(.format == "TV" or .format == "ONA" or .format == "MOVIE" or .format == "OVA" or .format == null) | .id // empty' 2>/dev/null | sort -nu)
		fi
		[[ ${#frontier[@]} -gt 0 ]] || break
		printf "%s\t - Sequel pass %s: %s new ids, %s to download\n" "$(date +%H:%M:%S)" "$pass" "${#frontier[@]}" "$(wc -l < "$pending")" | tee -a "$LOG"
		exec {fd}< "$pending"
		while mapfile -t -n "$batch_size" -u "$fd" chunk && [[ ${#chunk[@]} -gt 0 ]]
		do
			if ! anilist-fetch-batch "${chunk[@]}"
			then
				printf "%s\t - Batch failed, remaining ids will be downloaded one by one\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
				failed=1
				break
			fi
		done
		exec {fd}<&-
	done
	rm -f "$pending"
	printf "%s - Done\n\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
}
function get-anilist-infos () {
	local cache_file="$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json"
	local tmp_file="$cache_file.tmp"
	local cache_status
	local cache_ttl

	cache_status=$(jq -er '.data.Media | select(type == "object") | .status | strings | select(length > 0)' "$cache_file" 2>/dev/null)
	cache_ttl=$(cache-time-for-status "$cache_status") || exit 1
	if [[ -n "$cache_status" ]] && cache-is-fresh "$cache_file" "$cache_ttl"
	then
		return 0
	fi

	if [[ -z "$cache_status" ]] || ! cache-is-fresh "$cache_file" "$cache_ttl"
	then
		wait_time=0
		anilist_api_retry=0
		while [ $wait_time -lt 5 ] || [ $anilist_api_retry -lt 5 ];
		do
			if [[ "$airing_loop" == 1 ]]
			then
				printf "%s\t\t\t - Downloading airing info for Anilist : %s\n" "$(date +%H:%M:%S)" "$anilist_id" | tee -a "$LOG"
			elif [[ $cours_count_total -gt 1 ]]
			then
				printf "%s\t\t - Downloading data for S%s part-%s Anilist : %s\n" "$(date +%H:%M:%S)" "$season_number" "$cours_count" "$anilist_id" | tee -a "$LOG"
			elif [[ "$season_loop" == 1 ]]
			then
				printf "%s\t\t - Downloading data for S%s anilist : %s\n" "$(date +%H:%M:%S)" "$season_number" "$anilist_id" | tee -a "$LOG"
			else
				printf "%s\t\t - Downloading data for anilist : %s\n" "$(date +%H:%M:%S)" "$anilist_id" | tee -a "$LOG"
			fi
			if curl -sS 'https://graphql.anilist.co/' \
			-X POST \
			-H 'content-type: application/json' \
			--data '{ "query": "{ Media(type: ANIME, id: '"$anilist_id"') { relations { edges { relationType node { id type format title { romaji } status } } } title { romaji(stylised: false) english(stylised: false) native(stylised: false) } averageScore genres tags { name rank } studios { edges { node { name isAnimationStudio } } } startDate { year month } season seasonYear coverImage { extraLarge } status idMal} }" }' > "$tmp_file" -D "$SCRIPT_FOLDER/config/tmp/anilist-limit-rate.txt"
			then
				if jq -e '.data.Media | type == "object" and (.status | type == "string")' "$tmp_file" >/dev/null 2>&1
				then
					if ! mv "$tmp_file" "$cache_file"
					then
						printf "%s - Error saving AniList data for %s\n" "$(date +%H:%M:%S)" "$anilist_id" | tee -a "$LOG"
						exit 1
					fi
				else
					((anilist_api_retry++))
					rm -f "$tmp_file"
					printf "%s - Invalid json from AniList API down, waiting 60s\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
					sleep 61
					if [[ $anilist_api_retry == 4 ]]
					then
						printf "%s - Error can't download anilist data stopping script\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
						exit 1
					fi
					continue
				fi
			else
				((anilist_api_retry++))
				rm -f "$tmp_file"
				printf "%s - AniList request failed, waiting 60s\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
				sleep 61
				if [[ $anilist_api_retry == 4 ]]
				then
					printf "%s - Error can't download anilist data stopping script\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
					exit 1
				fi
				continue
			fi
			rate_limit=0
			rate_limit=$(grep -oP '(?<=x-ratelimit-remaining: )[0-9]+' "$SCRIPT_FOLDER/config/tmp/anilist-limit-rate.txt")
			((wait_time++))
			if [[ $wait_time == 4 ]] || [[ $anilist_api_retry == 4 ]]
			then
				printf "%s - Error can't download anilist data stopping script\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
				exit 1
			elif [[ -z $rate_limit ]]
			then
				printf "%s\t\t - Cloudflare limit rate reached watiting 60s\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
				sleep 61
			elif [[ $rate_limit -ge 3 ]]
			then
				sleep 1
				if [[ "$airing_loop" == 1 ]]
				then
					printf "%s\t\t\t - Done\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
				else
					printf "%s\t\t - Done\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
				fi
				break
			elif [[ $rate_limit -lt 3 ]]
			then
				printf "%s\t\t - Anilist API limit reached watiting 30s" "$(date +%H:%M:%S)" | tee -a "$LOG"
				sleep 30
				if [[ "$airing_loop" == 1 ]]
				then
					printf "%s\t\t\t - Done\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
				else
					printf "%s\t\t - Done\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
				fi
				break
			fi
		done
	fi
}
function mal-fetch-one () {
	local mal_id="$1"
	local cache_file="$SCRIPT_FOLDER/config/data/MAL-$mal_id.json"
	local tmp_file="$cache_file.$$.tmp"
	local attempt=0
	local status

	while [[ $attempt -lt 3 ]]
	do
		((attempt++))
		status=$(curl -sS --connect-timeout 10 --max-time 30 -o "$tmp_file" -w "%{http_code}" "https://malapi.animemap.dev/v4/anime/$mal_id") || status=000
		if [[ "$status" == "200" ]] && jq -e '.data | type == "object"' "$tmp_file" >/dev/null 2>&1
		then
			mv "$tmp_file" "$cache_file"
			sleep "${MAL_WORKER_DELAY:-2}"
			return 0
		fi
		printf "%s\t - MAL %s attempt %s/3 failed (HTTP %s)\n" "$(date +%H:%M:%S)" "$mal_id" "$attempt" "$status" >> "$LOG"
		if [[ "$status" == "429" ]]
		then
			sleep 30
		else
			sleep $((attempt * 5))
		fi
	done
	rm -f "$tmp_file"
	return 0
}
# Warm the MAL cache with a small worker pool; misses fall back to get-mal-infos
function prefetch-mal-infos () {
	local media_kind="$1"
	local pending="$SCRIPT_FOLDER/config/tmp/mal-prefetch-ids.txt"
	local workers="${MAL_WORKERS:-3}"
	local id
	local status
	local ttl
	local mal

	[[ "$workers" =~ ^[0-9]+$ ]] && [[ $workers -ge 1 ]] || workers=3
	printf "%s - Prefetching MAL data\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
	: > "$pending"
	while read -r id
	do
		[[ "$id" =~ ^[0-9]+$ ]] || continue
		IFS=$'\t' read -r status mal < <(jq -r '.data.Media | select(type == "object") | [.status, (.idMal // "")] | @tsv' "$SCRIPT_FOLDER/config/data/anilist-$id.json" 2>/dev/null)
		[[ -n "$status" ]] || continue
		[[ -n "$mal" ]] || mal=$(awk -F '\t' -v key="$id" '$1 == key { print $2; exit }' "$SCRIPT_FOLDER/config/tmp/anilist-mal.tsv")
		[[ "$mal" =~ ^[0-9]+$ ]] || continue
		ttl=$(cache-time-for-status "$status") || continue
		if [[ -s "$SCRIPT_FOLDER/config/data/MAL-$mal.json" ]] && jq -e '.data | type == "object"' "$SCRIPT_FOLDER/config/data/MAL-$mal.json" >/dev/null 2>&1 && cache-is-fresh "$SCRIPT_FOLDER/config/data/MAL-$mal.json" "$ttl"
		then
			continue
		fi
		printf '%s\n' "$mal" >> "$pending"
	done < <(collect-anilist-ids "$media_kind" | sort -nu)
	sort -nu "$pending" -o "$pending"
	printf "%s\t - %s MAL ids to download with %s workers\n" "$(date +%H:%M:%S)" "$(wc -l < "$pending")" "$workers" | tee -a "$LOG"
	export -f mal-fetch-one
	export SCRIPT_FOLDER LOG MAL_WORKER_DELAY
	xargs -a "$pending" -P "$workers" -n 1 bash -c 'mal-fetch-one "$1"' _
	rm -f "$pending"
	printf "%s - Done\n\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
}
function get-mal-infos () {
	mal_id=""
	get-mal-id
	if [[ $mal_id == 'null' ]] || [[ -z $mal_id ]]
	then
		printf "%s\t\t - Missing MAL ID for Anilist : %s / %s\n" "$(date +%H:%M:%S)" "$anilist_id" "$plex_title" | tee -a "$LOG"
		printf "%s - Missing MAL ID for Anilist : %s / %s\n" "$(date +%H:%M:%S)" "$anilist_id" "$plex_title" >> "$MATCH_LOG"
	else
		local cache_file="$SCRIPT_FOLDER/config/data/MAL-$mal_id.json"
		local tmp_file="$cache_file.tmp"
		local cache_status
		local cache_ttl
		local cached_valid=0
		local status

		cache_status=$(jq -er '.data.Media | select(type == "object") | .status | strings | select(length > 0)' "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json" 2>/dev/null)
		cache_ttl=$(cache-time-for-status "$cache_status") || exit 1
		if [[ -s "$cache_file" ]] && jq -e '.data | type == "object"' "$cache_file" >/dev/null 2>&1
		then
			cached_valid=1
			if cache-is-fresh "$cache_file" "$cache_ttl"
			then
				return 0
			fi
		fi
		if [[ $cached_valid == 0 ]] || ! cache-is-fresh "$cache_file" "$cache_ttl"
		then
			if [[ $cours_count_total -gt 1 ]]
			then
				printf "%s\t\t - Downloading data for S%s part-%s MAL : %s\n" "$(date +%H:%M:%S)" "$season_number" "$cours_count" "$mal_id" | tee -a "$LOG"
			elif [[ "$season_loop" == 1 ]]
			then
				printf "%s\t\t - Downloading data for S%s MAL : %s\n" "$(date +%H:%M:%S)" "$season_number" "$mal_id" | tee -a "$LOG"
			else
				printf "%s\t\t - Downloading data for MAL : %s\n" "$(date +%H:%M:%S)" "$mal_id" | tee -a "$LOG"
			fi
			status=$(curl -sS -o "$tmp_file" -w "%{http_code}" "https://malapi.animemap.dev/v4/anime/$mal_id") || status=000
			if [[ "$status" == "429" ]]
			then
				printf "%s - Jikan API limit reached watiting 30s" "$(date +%H:%M:%S)" | tee -a "$LOG"
				sleep 30
				status=$(curl -sS -o "$tmp_file" -w "%{http_code}" "https://malapi.animemap.dev/v4/anime/$mal_id") || status=000
			fi
			if [[ "$status" == "200" ]] && jq -e '.data | type == "object"' "$tmp_file" >/dev/null 2>&1
			then
				if ! mv "$tmp_file" "$cache_file"
				then
					printf "%s - Error saving MAL data for %s\n" "$(date +%H:%M:%S)" "$mal_id" | tee -a "$LOG"
					exit 1
				fi
			elif [[ $cached_valid == 1 ]]
			then
				rm -f "$tmp_file"
				printf "%s - MAL refresh failed, retaining cached data for MAL : %s\n" "$(date +%H:%M:%S)" "$mal_id" | tee -a "$LOG"
			else
				rm -f "$tmp_file"
				printf "%s - Error can't download valid MAL data for MAL : %s\n" "$(date +%H:%M:%S)" "$mal_id" | tee -a "$LOG"
				exit 1
			fi
			sleep 1.1
			printf "%s\t\t - Done\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
		fi
	fi
}
function get-romaji-title () {
	title="null"
	title_tmp="null"
	if tsv-has-value "$SCRIPT_FOLDER/config/$OVERRIDE" 2 "$anilist_id"
	then
		title_tmp=$(tsv-get-field "$SCRIPT_FOLDER/config/$OVERRIDE" 2 "$anilist_id" 3)
		if [[ -z "$title_tmp" ]]
		then
			title=$(jq '.data.Media.title.romaji' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json")
			less-caps-title
			echo "$title"
		else
			title="$title_tmp"
			less-caps-title
			echo "$title"
		fi
	else
		title=$(jq '.data.Media.title.romaji' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json")
		less-caps-title
		echo "$title"
	fi
}
function get-english-title () {
	title="null"
	title_tmp="null"
	if tsv-has-value "$SCRIPT_FOLDER/config/$OVERRIDE" 2 "$anilist_id"
	then
		title_tmp=$(tsv-get-field "$SCRIPT_FOLDER/config/$OVERRIDE" 2 "$anilist_id" 3)
		if [[ -z "$title_tmp" ]]
		then
			title=$(jq '.data.Media.title.english' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json")
			less-caps-title
			echo "$title"
		else
			title="$title_tmp"
			less-caps-title
			echo "$title"
		fi
	else
		title=$(jq '.data.Media.title.english' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json")
		less-caps-title
		echo "$title"
	fi
}
function get-native-title () {
	title=$(jq '.data.Media.title.native' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json")
	echo "$title"
}
function less-caps-title () {
	if [[ $REDUCE_TITLE_CAPS == "Yes" ]]
	then
		upper_check=$(echo "$title" | sed -e "s/[^ a-zA-Z]//g" -e 's/ //g')
		if [[ "$upper_check" =~ ^[A-Z]+$ ]]
		then
			title=$(echo "$title" | tr '[:upper:]' '[:lower:]' | sed "s/\( \|^\)\(.\)/\1\u\2/g")
		fi
	fi
}
function get-score () {
	anime_score=$(jq '.data.Media.averageScore' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json")
	if [[ "$anime_score" == "null" ]] || [[ "$anime_score" == "" ]]
	then
		anime_score=0
	else
		anime_score=$(printf %s "$anime_score" | awk '{print $1 / 10}')
	fi
}
function get-mal-score () {
mal_id=""
get-mal-id
if [[ $mal_id == 'null' ]] || [[ -z $mal_id ]]
then
	printf "%s\t\t - Missing MAL ID for Anilist : %s / %s\n" "$(date +%H:%M:%S)" "$anilist_id" "$plex_title" | tee -a "$LOG"
	printf "%s - Missing MAL ID for Anilist : %s / %s\n" "$(date +%H:%M:%S)" "$anilist_id" "$plex_title" >> "$MATCH_LOG"
else
	anime_score=0
	get-mal-infos
	anime_score=$(jq '.data.score' -r "$SCRIPT_FOLDER/config/data/MAL-$mal_id.json")
	if [[ "$anime_score" == "null" ]] || [[ "$anime_score" == "" ]]
	then
		anime_score=0
	fi
fi
}
function get-anilist-tags () {
	anime_tags=$( (jq '.data.Media.genres | .[]' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json" && jq --argjson anilist_tags_p "$ANILIST_TAGS_P" '.data.Media.tags | .[] | select( .rank >= $anilist_tags_p ) | .name' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json") | awk '{print $0}' | paste -sd ',')
}
function get-mal-tags () {
	mal_id=""
	get-mal-id
	if [[ $mal_id == 'null' ]] || [[ -z $mal_id ]]
	then
		printf "%s\t\t - Missing MAL ID for Anilist : %s / %s\n" "$(date +%H:%M:%S)" "$anilist_id" "$plex_title" | tee -a "$LOG"
		printf "%s - Missing MAL ID for Anilist : %s / %s\n" "$(date +%H:%M:%S)" "$anilist_id" "$plex_title" >> "$MATCH_LOG"
	else
		get-mal-infos
		anime_tags=$( (jq '.data.genres  | .[] | .name' -r "$SCRIPT_FOLDER/config/data/MAL-$mal_id.json" && jq '.data.demographics  | .[] | .name' -r "$SCRIPT_FOLDER/config/data/MAL-$mal_id.json" && jq '.data.themes  | .[] | .name' -r "$SCRIPT_FOLDER/config/data/MAL-$mal_id.json") | awk '{print $0}' | paste -sd ',')
	fi
}
function get-studios() {
	if tsv-has-value "$SCRIPT_FOLDER/config/$OVERRIDE" 2 "$anilist_id"
	then
		studio=$(tsv-get-field "$SCRIPT_FOLDER/config/$OVERRIDE" 2 "$anilist_id" 4)
		if [[ -z "$studio" ]]
		then
			studio=$(jq '.data.Media.studios.edges[].node | select( .isAnimationStudio == true ) | .name' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json" | head -n 1)
			if [[ -z "$studio" ]]
			then
				studio=$(jq '.data.Media.studios.edges[].node | .name' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json" | head -n 1)
			fi
		fi
	else
		studio=$(jq '.data.Media.studios.edges[].node | select( .isAnimationStudio == true ) | .name' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json" | head -n 1)
		if [[ -z "$studio" ]]
		then
			studio=$(jq '.data.Media.studios.edges[].node | .name' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json" | head -n 1)
		fi
	fi
}
function get-animes-season-year () {
	anime_season=$( (jq '.data.Media.seasonYear' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json" && jq '.data.Media.season' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json") | paste -sd ' ' | tr '[:upper:]' '[:lower:]' | sed "s/\( \|^\)\(.\)/\1\u\2/g")
	if [ "$anime_season" == "Null Null" ]
		then
		year_season=$(jq '.data.Media.startDate.year' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json")
		month_season=$(jq '.data.Media.startDate.month' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json")
		if [[ $month_season -le 3 ]]
		then
			name_season=Winter
		elif [[ $month_season -ge 4 && $month_season -le 6 ]]
		then
			name_season=Spring
		elif [[ $month_season -ge 7 && $month_season -le 9 ]]
		then
			name_season=Summer
		elif [[ $month_season -ge 10 ]]
		then
			name_season=Fall
		fi
		anime_season=$(printf "%s %s" "$year_season" "$name_season")
	fi
}
function get-animes-award () {
	award_check=""
	cr_awards=""
	if [[ $ANIME_AWARDS_NO_FVA == "Yes" ]]
	then
		award_check=$(jq --arg anilist_id "$anilist_id" '.[] | select( .anilist_id == $anilist_id ) | select(.cr_award | contains("English") or contains("Arabic") or contains("Spanish") or contains("Castilian") or contains("French")or contains("German") or contains("Italian") or contains("Portuguese") or contains("Russian")  | not) | "AA " + .year + " " + .cr_award' -r "$SCRIPT_FOLDER/config/data/cr-award.json" | paste -s -d, -)  > /dev/null
		if [[ -n $award_check ]]
		then
			cr_awards=$award_check
		fi
	else
		award_check=$(jq --arg anilist_id "$anilist_id" '.[] | select( .anilist_id == $anilist_id ) | "AA " + .year + " " + .cr_award' -r "$SCRIPT_FOLDER/config/data/cr-award.json" | paste -s -d, -)  > /dev/null
		if [[ -n $award_check ]]
		then
			cr_awards=$award_check
		fi
	fi
}
function get-airing-status () {
	if jq '.data.Media.status' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json" | grep -q -w "NOT_YET_RELEASED"
	then
		airing_status="Planned"
	else
		airing_loop=0
		anilist_backup_id=$anilist_id
		airing_status="Ended"
		last_sequel_found=0
		sequel_multi_check=0
		while [ $last_sequel_found -lt 50 ];
		do
			airing_loop=1
			if [[ $sequel_multi_check -gt 0 ]]
			then
				anilist_multi_id_backup=$anilist_id
				:> "$SCRIPT_FOLDER/config/tmp/airing_sequel_tmp.json"
				while IFS=$'\n' read -r anilist_id
				do
					get-anilist-infos
					cat "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json" >> "$SCRIPT_FOLDER/config/tmp/airing_sequel_tmp.json"
				done < "$SCRIPT_FOLDER/config/tmp/airing_sequel_tmp.txt"
				anilist_id=$anilist_multi_id_backup
				sequel_data=$(jq '.data.Media.relations.edges[] | select ( .relationType == "SEQUEL" ) | .node | select ( .format == "TV" or .format == "ONA" or .format == "MOVIE" or .format == "OVA" or .format == null )' -r "$SCRIPT_FOLDER/config/tmp/airing_sequel_tmp.json")
				if [ -z "$sequel_data" ]
				then
					airing_status="Ended"
					anilist_id=$anilist_backup_id
					break
				else
					sequel_check=$(printf "%s" "$sequel_data" | jq 'select ( .format == "TV" or .format == "ONA" or .format == "MOVIE" or .format == "OVA" or .format == null )')
					if echo "$sequel_check" | grep -q -w "NOT_YET_RELEASED"
					then
						airing_status="Planned"
						anilist_id=$anilist_backup_id
						break
					else
						anilist_id=$(printf "%s" "$sequel_data" | jq '.id')
						sequel_multi_check=$(printf %s "$anilist_id" | wc -l)
						if [[ $sequel_multi_check -gt 0 ]]
						then
							printf "%s" "$anilist_id" > "$SCRIPT_FOLDER/config/tmp/airing_sequel_tmp.txt"
							anilist_id=$( printf "%s" "$anilist_id" | head -n 1)
							((last_sequel_found++))
						else
							((last_sequel_found++))
						fi
					fi
				fi
			else
				get-anilist-infos
				sequel_data=$(jq '.data.Media.relations.edges[] | select ( .relationType == "SEQUEL" ) | .node | select ( .format == "TV" or .format == "ONA" or .format == "MOVIE" or .format == "OVA" or .format == null )' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json")
				if [ -z "$sequel_data" ]
				then
					airing_status="Ended"
					anilist_id=$anilist_backup_id
					break
				else
					sequel_check=$(printf "%s" "$sequel_data" | jq 'select ( .format == "TV" or .format == "ONA" or .format == "MOVIE" or .format == "OVA" or .format == null )')
					if echo "$sequel_check" | grep -q -w "NOT_YET_RELEASED"
					then
						airing_status="Planned"
						anilist_id=$anilist_backup_id
						break
					else
						anilist_id=$(printf "%s" "$sequel_data" | jq '.id')
						sequel_multi_check=$(printf %s "$anilist_id" | wc -l)
						if [[ $sequel_multi_check -gt 0 ]]
						then
							printf "%s\n" "$anilist_id" > "$SCRIPT_FOLDER/config/tmp/airing_sequel_tmp.txt"
							anilist_id=$( printf "%s" "$anilist_id" | head -n 1)
							((last_sequel_found++))
						else
							((last_sequel_found++))
						fi
					fi
				fi
			fi
		done
		airing_loop=0
		anilist_id=$anilist_backup_id
		if [[ $last_sequel_found -ge 50 ]]
		then
			airing_status="Ended"
		fi
	fi
}
function get-poster () {
	if [[ $POSTER_DOWNLOAD == "Yes" ]]
	then
		if [ ! -f "$ASSET_FOLDER/$asset_name/poster.jpg" ]
		then
			if [ ! -d "$ASSET_FOLDER/$asset_name" ]
			then
				mkdir "$ASSET_FOLDER/$asset_name"
			fi
			if [[ $POSTER_SOURCE == "MAL" ]]
			then
				get-mal-infos
				printf "%s\t\t - Downloading poster for MAL : %s\n" "$(date +%H:%M:%S)" "$mal_id" | tee -a "$LOG"
				poster_url=$(jq '.data.images.jpg.large_image_url' -r "$SCRIPT_FOLDER/config/data/MAL-$mal_id.json")
				curl -s "$poster_url" -o "$ASSET_FOLDER/$asset_name/poster.jpg"
				sleep 1.5
				printf "%s\t\t - Done\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
			else
				printf "%s\t\t - Downloading poster for anilist : %s\n" "$(date +%H:%M:%S)" "$anilist_id" | tee -a "$LOG"
				poster_url=$(jq '.data.Media.coverImage.extraLarge' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json")
				curl -s "$poster_url" -o "$ASSET_FOLDER/$asset_name/poster.jpg"
				sleep 0.5
				printf "%s\t\t - Done\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
			fi
		else
			postersize=$(du -b "$ASSET_FOLDER/$asset_name/poster.jpg" | awk '{ print $1 }')
			if [[ $postersize -lt 10000 ]]
			then
				rm "$ASSET_FOLDER/$asset_name/poster.jpg"
				if [ ! -d "$ASSET_FOLDER/$asset_name" ]
				then
					mkdir "$ASSET_FOLDER/$asset_name"
				fi
				if [[ $POSTER_SOURCE == "MAL" ]]
				then
					get-mal-infos
					printf "%s\t\t - Downloading poster for MAL : %s\n" "$(date +%H:%M:%S)" "$mal_id" | tee -a "$LOG"
					poster_url=$(jq '.data.images.jpg.large_image_url' -r "$SCRIPT_FOLDER/config/data/MAL-$mal_id.json")
					curl -s "$poster_url" -o "$ASSET_FOLDER/$asset_name/poster.jpg"
					sleep 1.5
					printf "%s\t\t - Done\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
				else
					printf "%s\t\t - Downloading poster for anilist : %s\n" "$(date +%H:%M:%S)" "$anilist_id" | tee -a "$LOG"
					poster_url=$(jq '.data.Media.coverImage.extraLarge' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json")
					curl -s "$poster_url" -o "$ASSET_FOLDER/$asset_name/poster.jpg"
					sleep 0.5
					printf "%s\t\t - Done\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
				fi
			fi
		fi
	fi
}
function get-season-poster () {
	if [[ $POSTER_SEASON_DOWNLOAD == "Yes" ]]
	then
		if [[ $season_number -lt 10 ]]
		then
			assets_filepath="$ASSET_FOLDER/$asset_name/Season0$season_number.jpg"
		else
			assets_filepath="$ASSET_FOLDER/$asset_name/Season$season_number.jpg"
		fi
		if [ ! -f "$assets_filepath" ]
		then
			if [ ! -d "$ASSET_FOLDER/$asset_name" ]
			then
				mkdir "$ASSET_FOLDER/$asset_name"
			fi
			if [[ $POSTER_SOURCE == "MAL" ]]
			then
				get-mal-infos
				printf "%s\t\t - Downloading poster for S%s MAL : %s\n" "$(date +%H:%M:%S)" "$season_number" "$mal_id" | tee -a "$LOG"
				poster_url=$(jq '.data.images.jpg.large_image_url' -r "$SCRIPT_FOLDER/config/data/MAL-$mal_id.json")
				curl -s "$poster_url" -o "$assets_filepath"
				sleep 1.5
				printf "%s\t\t - Done\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
			else
				printf "%s\t\t - Downloading poster for S%s anilist : %s\n" "$(date +%H:%M:%S)" "$season_number" "$anilist_id" | tee -a "$LOG"
				poster_url=$(jq '.data.Media.coverImage.extraLarge' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json")
				curl -s "$poster_url" -o "$assets_filepath"
				sleep 0.5
				printf "%s\t\t - Done\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
			fi
		else
			postersize=$(du -b "$assets_filepath" | awk '{ print $1 }')
			if [[ $postersize -lt 10000 ]]
			then
				rm "$assets_filepath"
				if [ ! -d "$ASSET_FOLDER/$asset_name" ]
				then
					mkdir "$ASSET_FOLDER/$asset_name"
				fi
				if [[ $POSTER_SOURCE == "MAL" ]]
				then
					get-mal-infos
					printf "%s\t\t - Downloading poster S%s MAL : %s\n" "$(date +%H:%M:%S)" "$season_number" "$mal_id" | tee -a "$LOG"
					poster_url=$(jq '.data.images.jpg.large_image_url' -r "$SCRIPT_FOLDER/config/data/MAL-$mal_id.json")
					curl -s "$poster_url" -o "$assets_filepath"
					sleep 1.5
					printf "%s\t\t - Done\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
				else
					printf "%s\t\t - Downloading poster S%s anilist : %s\n" "$(date +%H:%M:%S)" "$season_number" "$anilist_id" | tee -a "$LOG"
					poster_url=$(jq '.data.Media.coverImage.extraLarge' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json")
					curl -s "$poster_url" -o "$assets_filepath"
					sleep 0.5
					printf "%s\t\t - Done\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
				fi
			fi
		fi
	fi
}
function get-rating-1 () {
	if [[ $RATING_1_SOURCE == "ANILIST" || $RATING_1_SOURCE == "MAL" ]]
	then
		if [[ $RATING_1_SOURCE == "ANILIST" ]]
		then
			get-score
			score_1=$anime_score
		else
			get-mal-score
			score_1=$anime_score
		fi
	fi
	if [[ "$score_1" == 0 ]]
	then
		if [[ $RATING_1_SOURCE == "ANILIST" ]]
		then
			printf "%s\t\t - invalid rating for Anilist : %s skipping \n" "$(date +%H:%M:%S)" "$anilist_id" | tee -a "$LOG"
		else 
			printf "%s\t\t - invalid rating for MAL : %s skipping \n" "$(date +%H:%M:%S)" "$mal_id" | tee -a "$LOG"
		fi
	else
		score_1=$(printf '%.*f\n' 1 "$score_1")
		printf "    %s_rating: %s\n" "$RATING_1_TYPE" "$score_1" >> "$METADATA"
	fi
}
function get-season-rating-1 () {
	if [[ $RATING_1_SOURCE == "ANILIST" || $RATING_1_SOURCE == "MAL" ]]
	then
		if [[ $RATING_1_SOURCE == "ANILIST" ]]
		then
			get-score
			score_1_season=$anime_score
		else
			get-mal-score
			score_1_season=$anime_score
		fi
		score_1_season=$(printf '%.*f\n' 1 "$score_1_season")
		if [[ "$score_1_season" == 0.0 ]]
		then
			((score_1_no_rating_seasons++))
		fi
	fi
}
function get-cour-rating-1 () {
	if [[ $RATING_1_SOURCE == "ANILIST" || $RATING_1_SOURCE == "MAL" ]]
	then
		if [[ $RATING_1_SOURCE == "ANILIST" ]]
		then
			get-score
			score_1_cour=$anime_score
		else
			get-mal-score
			score_1_cour=$anime_score
		fi
		score_1_cour=$(printf '%.*f\n' 1 "$score_1_cour")
		if [[ "$score_1_cour" == 0.0 ]]
		then
			((score_1_no_rating_cours++))
		fi
	fi
}
function total-cour-rating-1 () {
	if [[ $RATING_1_SOURCE == "ANILIST" || $RATING_1_SOURCE == "MAL" ]]
	then
		total_1_cours_score=$(echo | awk -v v1="$score_1_cour" -v v2="$total_1_cours_score" '{print v1 + v2}')
	fi
}
function total-rating-1 () {
	if [[ $RATING_1_SOURCE == "ANILIST" || $RATING_1_SOURCE == "MAL" ]]
	then
		total_1_score=$(echo | awk -v v1="$score_1_season" -v v2="$total_1_score" '{print v1 + v2}')
	fi
}
function check-rating-1-valid () {
	if [[ $RATING_1_SOURCE == "ANILIST" || $RATING_1_SOURCE == "MAL" ]]
	then
		if [[ "$score_1" == 0 ]]
		then
			if [[ $RATING_1_SOURCE == "ANILIST" ]]
			then
				printf "%s\t\t - invalid rating for Anilist : %s skipping \n" "$(date +%H:%M:%S)" "$anilist_id" | tee -a "$LOG"
			else
				get-mal-id
				if [[ $mal_id != 'null' ]] || [[ -n $mal_id ]]
				then
					printf "%s\t\t - invalid rating for MAL : %s skipping \n" "$(date +%H:%M:%S)" "$mal_id" | tee -a "$LOG"
				fi
			fi
		else
			score_1=$(printf '%.*f\n' 1 "$score_1")
			printf "    %s_rating: %s\n" "$RATING_1_TYPE" "$score_1" >> "$METADATA"
		fi
	fi
}
function get-rating-2 () {
	if [[ $RATING_2_SOURCE == "ANILIST" || $RATING_2_SOURCE == "MAL" ]]
	then
		if [[ $RATING_2_SOURCE == "ANILIST" ]]
		then
			get-score
			score_2=$anime_score
		else
			get-mal-score
			score_2=$anime_score
		fi
	fi
	if [[ "$score_2" == 0 ]]
	then
		if [[ $RATING_2_SOURCE == "ANILIST" ]]
		then
			printf "%s\t\t - invalid rating for Anilist : %s skipping \n" "$(date +%H:%M:%S)" "$anilist_id" | tee -a "$LOG"
		else 
			printf "%s\t\t - invalid rating for MAL : %s skipping \n" "$(date +%H:%M:%S)" "$mal_id" | tee -a "$LOG"
		fi
	else
		score_2=$(printf '%.*f\n' 1 "$score_2")
		printf "    %s_rating: %s\n" "$RATING_2_TYPE" "$score_2" >> "$METADATA"
	fi
}
function get-season-rating-2 () {
	if [[ $RATING_2_SOURCE == "ANILIST" || $RATING_2_SOURCE == "MAL" ]]
	then
		if [[ $RATING_2_SOURCE == "ANILIST" ]]
		then
			get-score
			score_2_season=$anime_score
		else
			get-mal-score
			score_2_season=$anime_score
		fi
		score_2_season=$(printf '%.*f\n' 1 "$score_2_season")
		if [[ "$score_2_season" == 0.0 ]]
		then
			((score_2_no_rating_seasons++))
		fi
	fi
}
function get-cour-rating-2 () {
	if [[ $RATING_2_SOURCE == "ANILIST" || $RATING_2_SOURCE == "MAL" ]]
	then
		if [[ $RATING_2_SOURCE == "ANILIST" ]]
		then
			get-score
			score_2_cour=$anime_score
		else
			get-mal-score
			score_2_cour=$anime_score
		fi
		score_2_cour=$(printf '%.*f\n' 1 "$score_2_cour")
		if [[ "$score_2_cour" == 0.0 ]]
		then
			((score_2_no_rating_cours++))
		fi
	fi
}
function total-cour-rating-2 () {
	if [[ $RATING_2_SOURCE == "ANILIST" || $RATING_2_SOURCE == "MAL" ]]
	then
		total_2_cours_score=$(echo | awk -v v1="$score_2_cour" -v v2="$total_2_cours_score" '{print v1 + v2}')
	fi
}
function total-rating-2 () {
	if [[ $RATING_2_SOURCE == "ANILIST" || $RATING_2_SOURCE == "MAL" ]]
	then
		total_2_score=$(echo | awk -v v1="$score_2_season" -v v2="$total_2_score" '{print v1 + v2}')
	fi
}
function check-rating-2-valid () {
	if [[ $RATING_2_SOURCE == "ANILIST" || $RATING_2_SOURCE == "MAL" ]]
	then
		if [[ "$score_2" == 0 ]]
		then
			if [[ $RATING_2_SOURCE == "ANILIST" ]]
			then
				printf "%s\t\t - invalid rating for Anilist : %s skipping \n" "$(date +%H:%M:%S)" "$anilist_id" | tee -a "$LOG"
			else
				get-mal-id
				if [[ $mal_id == 'null' ]] || [[ -n $mal_id ]]
				then
					printf "%s\t\t - invalid rating for MAL : %s skipping \n" "$(date +%H:%M:%S)" "$mal_id" | tee -a "$LOG"
				fi
			fi
		else
			score_2=$(printf '%.*f\n' 1 "$score_2")
			printf "    %s_rating: %s\n" "$RATING_2_TYPE" "$score_2" >> "$METADATA"
		fi
	fi
}
function get-season-infos () {
	override_id=""
	anilist_backup_id=$anilist_id
	season_check=$(awk -F '\t' -v key="$anilist_id" '$1 == key { print $3 }' "$SCRIPT_FOLDER/config/tmp/anime-mapping.tsv")
	first_season=$(echo "$seasons_list" | awk -F "," '{print $1}')
	last_season=$(echo "$seasons_list" | awk -F "," '{print $NF}')
	total_seasons=$(echo "$seasons_list" | awk -F "," '{print NF}')
	valid_anilist_id=$(awk -F '\t' -v key="$tvdb_id" '$2 == key { print $1 }' "$SCRIPT_FOLDER/config/tmp/anime-mapping.tsv")
	if tsv-has-value "$SCRIPT_FOLDER/config/$OVERRIDE" 2 "$anilist_backup_id" && [[ $last_season -eq 1 ]]
	then
		valid_anilist_id=1
		override_id=$anilist_backup_id
	fi
	if [[ "$first_season" -eq 0 ]]
	then
		total_seasons=$((total_seasons - 1))
	fi
	if [ -n "$valid_anilist_id" ] && [[ $season_check != -1 ]]
	then
		total_1_score=0
		total_2_score=0
		score_1_season=0
		score_2_season=0
		score_1_no_rating_seasons=0
		score_2_no_rating_seasons=0
		season_loop=0
		anime_season=""
		award_check=""
		cr_awards=""
		printf "    seasons:\n" >> "$METADATA"
		IFS=","
		for season_number in $seasons_list
		do
			if [[ $season_number -eq 0 ]]
			then
				printf "      0:\n        label.remove: Score\n" >> "$METADATA"
			else
				season_loop=1
				anilist_ids=$(awk -F '\t' -v tvdb="$tvdb_id" -v season="$season_number" '$2 == tvdb && $3 == season { print $4 "\t" $1 }' "$SCRIPT_FOLDER/config/tmp/anime-mapping.tsv" | sort -s -t $'\t' -k1,1 | cut -f2 | paste -s -d, -)
				if [ -n "$override_id" ] && [[ $season_number -eq 1 ]]
				then
					anilist_ids=$anilist_backup_id
				fi
				cours_count_total=$(printf %s "$anilist_ids" | awk -F "," '{print NF}')
				total_1_cours_score=0
				total_2_cours_score=0
				score_1_no_rating_cours=0
				score_2_no_rating_cours=0
				cours_count=0
				cour_status=""
				all_cours_anime_season=""
				season_userlist_type_add=""
				seasons_userlist_type_remove=""
				IFS=','
				for anilist_id in $anilist_ids
				do
					((cours_count++))
					if [[ -n "$anilist_id" ]]
					then
						get-anilist-infos
						if jq '.data.Media.status' -r "$SCRIPT_FOLDER/config/data/anilist-$anilist_id.json" | grep -q -w "NOT_YET_RELEASED"
						then
							((score_1_no_rating_cours++))
							((score_2_no_rating_cours++))
							continue
						fi
						if { [[ $ANILIST_LISTS_LEVEL == "season" ]] || [[ $ANILIST_LISTS_LEVEL == "both" ]]; } && [[ $ANILIST_LISTS == "Yes" ]]
						then
							for userlist_type in Completed Watching Dropped Paused Planning
							do
								if grep -q -w "$anilist_id" "$SCRIPT_FOLDER/config/data/anilist-$ANILIST_USERNAME-$userlist_type.tsv"
								then
									userlist_type_count=$(printf %s "$season_userlist_type_add" | awk -F "," '{print NF}')
									if [[ $userlist_type_count -gt 1 ]]
									then
										season_userlist_type_add=$(printf "%s,%s" "$season_userlist_type_add" "$userlist_type")
									else
										season_userlist_type_add="$userlist_type"
									fi
								fi
							done
						fi
						get-cour-rating-1
						get-cour-rating-2
						if [[ $ANIME_AWARDS == "Yes" ]]
						then
							get-animes-award
						fi
						if [[ $SEASON_YEAR == "Yes" ]]
						then
							get-animes-season-year
							if [[ $cours_count -gt 1 ]]
							then
								all_cours_anime_season=$(printf "%s,%s" "$anime_season" "$all_cours_anime_season")
							else
								all_cours_anime_season=$anime_season
							fi
						fi
					else
						printf "%s\t\t - Missing Anilist ID for tvdb : %s - Season : %s cour : %s / %s\n" "$(date +%H:%M:%S)" "$tvdb_id" "$season_number" "$cours_count" "$plex_title" | tee -a "$LOG"
						printf "%s\t\t - Missing Anilist ID for tvdb : %s - Season : %s cour : %s / %s\n" "$(date +%H:%M:%S)" "$tvdb_id" "$season_number" "$cours_count" "$plex_title" >> "$MATCH_LOG"
						((score_1_no_rating_cours++))
						((score_2_no_rating_cours++))
					fi
					total-cour-rating-1
					total-cour-rating-2
				done
				anime_season=$all_cours_anime_season
				if [[ $RATING_1_SOURCE == "ANILIST" || $RATING_1_SOURCE == "MAL" ]]
				then
					if [[ "$total_1_cours_score" != 0 ]]
					then
						total_1_cours=$((cours_count - score_1_no_rating_cours))
						if [[ "$total_1_cours" != 0 ]]
						then
							score_1_season=$(echo | awk -v v1="$total_1_cours_score" -v v2="$total_1_cours" '{print v1 / v2}')
							score_1_season=$(printf '%.*f\n' 1 "$score_1_season")
						else
							score_1_season=0
							((score_1_no_rating_seasons++))
						fi
					else
						score_1_season=0
						((score_1_no_rating_seasons++))
					fi
				fi
				if [[ $RATING_2_SOURCE == "ANILIST" || $RATING_2_SOURCE == "MAL" ]]
				then
					if [[ "$total_2_cours_score" != 0 ]]
					then
						total_2_cours=$((cours_count - score_2_no_rating_cours))
						if [[ "$total_2_cours" != 0 ]]
						then
							score_2_season=$(echo | awk -v v1="$total_2_cours_score" -v v2="$total_2_cours" '{print v1 / v2}')
							score_2_season=$(printf '%.*f\n' 1 "$score_2_season")
						else
							score_2_season=0
							((score_2_no_rating_seasons++))
						fi
					else
						score_1_season=0
						((score_1_no_rating_seasons++))
					fi
				fi
				cours_count_total=0
				anilist_id=$(awk -F '\t' -v tvdb="$tvdb_id" -v season="$season_number" '$2 == tvdb && $3 == season && $4 == "0" { print $1; exit }' "$SCRIPT_FOLDER/config/tmp/anime-mapping.tsv")
				if [ -n "$override_id" ] && [[ $season_number -eq 1 ]]
				then
					anilist_id=$anilist_backup_id 
				fi
				if [[ -z "$anilist_id" ]]
				then
					printf "%s\t\t - Missing Anilist ID for tvdb : %s - Season : %s / %s\n" "$(date +%H:%M:%S)" "$tvdb_id" "$season_number" "$plex_title" | tee -a "$LOG"
					printf "%s - Missing Anilist ID for tvdb : %s - Season : %s / %s\n" "$(date +%H:%M:%S)" "$tvdb_id" "$season_number" "$plex_title" >> "$MATCH_LOG"
					((score_1_no_rating_seasons++))
					((score_2_no_rating_seasons++))
				else
					printf "      %s:\n" "$season_number"  >> "$METADATA"
					romaji_title=$(get-romaji-title)
					english_title=$(get-english-title)
					if [ "$english_title" == "null" ]
					then
						english_title=$romaji_title
					fi
					if [[ $ALLOW_RENAMING == "Yes" && $RENAME_SEASONS == "Yes" ]]
					then
						if [[ $MAIN_TITLE_ENG == "Yes" ]]
						then
						printf "        title: |-\n          %s\n" "$english_title" >> "$METADATA"
						else
						printf "        title: |-\n          %s\n" "$romaji_title" >> "$METADATA"
						fi
					fi
					season_label_add=""
					season_label_remove=""
					if [[ -n "$cr_awards" ]]
					then
						if [[ -n "$season_label_add" ]]
						then
							season_label_add=$(printf "%s,AA Winner" "$season_label_add")
						else
							season_label_add="AA Winner"
						fi
					fi
					if { [[ $ANILIST_LISTS_LEVEL == "season" ]] || [[ $ANILIST_LISTS_LEVEL == "both" ]]; } && [[ $ANILIST_LISTS == "Yes" ]]
					then
						seasons_userlist_type_remove="Completed,Watching,Dropped,Paused,Planning"
						userlist_type_count=$(printf %s "$season_userlist_type_add" | awk -F "," '{print NF}')
						if [[ -n $season_userlist_type_add ]] && [[ $userlist_type_count -gt 0 ]]
						then
							IFS=","
							for userlist_type in $season_userlist_type_add
							do
								seasons_userlist_type_remove=$(printf "%s" "$seasons_userlist_type_remove" | sed s/"$userlist_type"// | sed 's/^,//' | sed 's/,,/,/g')
							done
						fi
						if [[ -n "$season_userlist_type_add" ]]
						then
							if [[ -n "$season_label_add" ]]
							then
								season_label_add=$(printf "%s,%s" "$season_label_add" "$season_userlist_type_add")
							else
								season_label_add="$season_userlist_type_add"
							fi
						fi
						if [[ -n "$seasons_userlist_type_remove" ]]
						then
							if [[ -n "$season_label_remove" ]]
							then
								season_label_remove=$(printf "%s,%s" "$season_label_remove" "$seasons_userlist_type_remove")
							else
								season_label_remove="$seasons_userlist_type_remove"
							fi
						fi
					fi
					if [[ -n "$anime_season" ]]
					then
						if [[ -n "$season_label_add" ]]
						then
							season_label_add=$(printf "%s,%s" "$season_label_add" "$anime_season")
						else
							season_label_add="$anime_season"
						fi
					fi
					if [[ -n "$season_label_add" ]]
					then
						if [[ $last_season -eq 1 ]]
						then
							if [[ $IGNORE_S1_ONLY_RATING == "Yes" ]] || [[ $IGNORE_SEASONS_RATING == "Yes" ]]
							then
								printf "        label: %s\n" "$season_label_add" >> "$METADATA"
							else
								printf "        label: Score,%s\n" "$season_label_add" >> "$METADATA"
							fi
						elif [[ $IGNORE_SEASONS_RATING == "Yes" ]]
						then	
							printf "        label: %s\n" "$season_label_add" >> "$METADATA"
						else
							printf "        label: Score,%s\n" "$season_label_add" >> "$METADATA"
						fi
					else
						if [[ $last_season -eq 1 ]]
						then
							if [[ $IGNORE_S1_ONLY_RATING != "Yes" ]] || [[ $IGNORE_SEASONS_RATING != "Yes" ]]
							then
								printf "        label: Score\n" >> "$METADATA"
							fi
						elif [[ $IGNORE_SEASONS_RATING != "Yes" ]]
						then	
							printf "        label: Score\n" >> "$METADATA"
						fi
					fi
					if [[ -n "$season_label_remove" ]]
					then
						if [[ $last_season -eq 1 ]]
						then
							if [[ $IGNORE_S1_ONLY_RATING == "Yes" ]] || [[ $IGNORE_SEASONS_RATING == "Yes" ]]
							then
								printf "        label.remove: Score,%s\n" "$season_label_remove" >> "$METADATA"
							else
								printf "        label.remove: %s\n" "$season_label_remove" >> "$METADATA"
							fi
						elif [[ $IGNORE_SEASONS_RATING == "Yes" ]]
						then
							printf "        label.remove: Score,%s\n" "$season_label_remove" >> "$METADATA"
						else
							printf "        label.remove: %s\n" "$season_label_remove" >> "$METADATA"
						fi
					else
						if [[ $last_season -eq 1 ]]
						then
							if [[ $IGNORE_S1_ONLY_RATING == "Yes" ]] || [[ $IGNORE_SEASONS_RATING == "Yes" ]]
							then
							printf "        label.remove: Score\n" >> "$METADATA"
							fi
						elif [[ $IGNORE_SEASONS_RATING == "Yes" ]]
						then
							printf "        label.remove: Score\n" >> "$METADATA"
						fi
					fi
					if [[ $IGNORE_SEASONS_RATING != "Yes" ]]
					then
						if [[ $last_season -eq 1 ]]
						then
							if [[ $IGNORE_S1_ONLY_RATING != "Yes" ]]
							then
							printf "        user_rating: %s\n" "$score_1_season" >> "$METADATA"
							fi
						else
							printf "        user_rating: %s\n" "$score_1_season" >> "$METADATA"
						fi
					fi
					total-rating-1
					total-rating-2
					get-season-poster
				fi
			fi
		done
		season_loop=0
		if [[ $RATING_1_SOURCE == "ANILIST" || $RATING_1_SOURCE == "MAL" ]]
		then
			if [[ "$total_1_score" != 0 ]]
			then
				total_1_seasons=$((total_seasons - score_1_no_rating_seasons))
				if [[ "$total_1_seasons" != 0 ]]
				then
					score_1=$(echo | awk -v v1="$total_1_score" -v v2="$total_1_seasons" '{print v1 / v2}')
					score_1=$(printf '%.*f\n' 1 "$score_1")
				else
					score_1=0
				fi
			else
				score_1=0
			fi
		fi
		if [[ $RATING_2_SOURCE == "ANILIST" || $RATING_2_SOURCE == "MAL" ]]
		then
			if [[ "$total_2_score" != 0 ]]
			then
				total_2_seasons=$((total_seasons - score_2_no_rating_seasons))
				if [[ "$total_2_seasons" != 0 ]]
				then
					score_2=$(echo | awk -v v1="$total_2_score" -v v2="$total_2_seasons" '{print v1 / v2}')
					score_2=$(printf '%.*f\n' 1 "$score_2")
				else
					score_2=0
				fi
			else
				score_2=0
			fi
		fi
	else
		if [[ $RATING_1_SOURCE == "ANILIST" || $RATING_1_SOURCE == "MAL" ]]
		then
			if [[ $RATING_1_SOURCE == "ANILIST" ]]
			then
				get-score
				score_1=$anime_score
			else
				get-mal-score
				score_1=$anime_score
			fi
		fi
		if [[ "$score_1" != 0 ]]
		then
			score_1=$(printf '%.*f\n' 1 "$score_1")
		fi
		if [[ $RATING_2_SOURCE == "ANILIST" || $RATING_2_SOURCE == "MAL" ]]
		then
			if [[ $RATING_2_SOURCE == "ANILIST" ]]
			then
				get-score
				score_2=$anime_score
			else
				get-mal-score
				score_2=$anime_score
			fi
		fi
		if [[ "$score_2" != 0 ]]
		then
			score_2=$(printf '%.*f\n' 1 "$score_2")
		fi
	fi
	anilist_id=$anilist_backup_id
}
function write-metadata () {
	get-anilist-infos
	if [[ $media_type == "animes" ]]
	then
		printf "  %s:\n" "$tvdb_id" >> "$METADATA"
	else
		printf "  %s:\n" "$imdb_id" >> "$METADATA"
	fi
	romaji_title=$(get-romaji-title)
	english_title=$(get-english-title)
	native_title=$(get-native-title)
	if [ "$english_title" == "null" ]
	then
		english_title=$romaji_title
	fi
	if [ "$native_title" == "null" ]
	then
		native_title=$romaji_title
	fi
	if [[ $ALLOW_RENAMING == "Yes" ]]
	then
		if [[ $MAIN_TITLE_ENG == "Yes" ]]
		then
			if [[ $ORIGINAL_TITLE_NATIVE == "Yes" ]]
			then
				printf "    title: |-\n      %s\n    sort_title: |-\n      %s\n    original_title: |-\n      %s\n" "$english_title" "$english_title" "$native_title" >> "$METADATA"
			else
				printf "    title: |-\n      %s\n    sort_title: |-\n      %s\n    original_title: |-\n      %s\n" "$english_title" "$english_title" "$romaji_title" >> "$METADATA"
			fi
		else
			printf "    title: |-\n      %s\n" "$romaji_title" >> "$METADATA"
			if [[ $SORT_TITLE_ENG == "Yes" ]]
			then
				printf "    sort_title: |-\n      %s\n" "$english_title" >> "$METADATA"
			else
				printf "    sort_title: |-\n      %s\n" "$romaji_title" >> "$METADATA"
			fi
			if [[ $ORIGINAL_TITLE_NATIVE == "Yes" ]]
			then
				printf "    original_title: |-\n      %s\n" "$native_title" >> "$METADATA"
			else
				printf "    original_title: |-\n      %s\n" "$english_title" >> "$METADATA"
			fi
		fi
	fi
	if [[ $DISABLE_TAGS != "Yes" ]]
	then
		if [[ "$TAG_SOURCE" == "MAL" ]]
		then
			get-mal-tags
		else
			get-anilist-tags
		fi
		if [[ "$ADD_ANIME_TAG" == "No" ]]
		then
			printf "    genre.sync: %s\n" "$anime_tags" >> "$METADATA"
		else
			printf "    genre.sync: Anime,%s\n" "$anime_tags" >> "$METADATA"
		fi
	fi
	get-studios
	if [[ -n "$studio" ]]
	then
		printf "    studio: %s\n" "$studio" >> "$METADATA"
	fi
	get-poster
	if [[ $media_type == "animes" ]]
	then
		if [[ $IGNORE_SEASONS == "Yes" ]] || [[ $override_seasons_ignore == "yes" ]]
		then
			get-rating-1
			get-rating-2
		else
			get-season-infos
			check-rating-1-valid
			check-rating-2-valid
		fi
	else
		get-rating-1
		get-rating-2
	fi
	label_add=""
	label_remove=""
	if [[ $media_type == "animes" ]]
	then
		printf "%s\t\t - Writing airing status\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
		if tsv-has-value "$SCRIPT_FOLDER/config/data/ongoing.tsv" 1 "$tvdb_id"
		then
			label_add="Airing"
			label_remove="Planned,Ended"
			printf "%s\t\t - Done\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
		else
			get-airing-status
			if [[ $airing_status == Planned ]]
			then
				label_add="Planned"
				label_remove="Airing,Ended"
				printf "%s\t\t - Done\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
			else
				label_add="Ended"
				label_remove="Planned,Airing"
				printf "%s\t\t - Done\n" "$(date +%H:%M:%S)" | tee -a "$LOG"
			fi
		fi
	fi
	if [[ -n $cr_awards ]]
	then
		if [[ -n "$label_add" ]]
		then
			label_add=$(printf "AA Winner,%s" "$label_add")
		else
			label_add="AA Winner"
		fi
	else
		get-animes-award
		if [[ -n $cr_awards ]]
		then
			if [[ -n "$label_add" ]]
			then
				label_add=$(printf "AA Winner,%s" "$label_add")
			else
				label_add="AA Winner"
			fi
		fi
	fi
	if { [[ $ANILIST_LISTS_LEVEL == "show" ]] || [[ $ANILIST_LISTS_LEVEL == "both" ]]; } && [[ $ANILIST_LISTS == "Yes" ]]
	then
		all_anilist_ids=""
		userlist_type_add=""
		userlist_type_remove="Completed,Watching,Dropped,Paused,Planning"
		for userlist_type in Completed Watching Dropped Paused Planning
		do
			if [[ $media_type == "animes" ]]
			then
				all_anilist_ids=$(awk -F '\t' -v key="$tvdb_id" '$2 == key { print $1 }' "$SCRIPT_FOLDER/config/tmp/anime-mapping.tsv" | paste -s -d, - | sed 's/,/\\|/g')
			else
				all_anilist_ids=$anilist_id
			fi
			if grep -q -w "$all_anilist_ids" "$SCRIPT_FOLDER/config/data/anilist-$ANILIST_USERNAME-$userlist_type.tsv"
			then
				userlist_type_count=$(printf %s "$userlist_type_add" | awk -F "," '{print NF}')
				if [[ $userlist_type_count -gt 1 ]]
				then
					userlist_type_add=$(printf "%s,%s" "$userlist_type_add" "$userlist_type")
				else
					userlist_type_add=$userlist_type
				fi
				userlist_type_remove=$(printf "%s" "$userlist_type_remove" | sed s/"$userlist_type"// | sed 's/^,//' | sed 's/,,/,/g')
			fi
		done
		if [[ -n "$userlist_type_add" ]]
		then
			if [[ -n "$label_add" ]]
			then
				label_add=$(printf "%s,%s" "$label_add" "$userlist_type_add")
			else
				label_add="$userlist_type_add"
			fi
		fi
		if [[ -n "$userlist_type_remove" ]]
		then
			if [[ -n "$label_remove" ]]
			then
				label_remove=$(printf "%s,%s" "$label_remove" "$userlist_type_remove")
			else
				label_remove="$userlist_type_remove"
			fi
		fi
	fi
	if [[ -n "$label_add" ]]
	then
		printf "    label: %s\n" "$label_add" >> "$METADATA"
	fi
	if [[ -n "$label_remove" ]]
	then
		printf "    label.remove: %s\n" "$label_remove" >> "$METADATA"
	fi
	tvdb_id=""
	imdb_id=""
	anilist_id=""
	mal_id=""
	override_seasons_ignore=""
	award_check=""
	cr_awards=""
}
