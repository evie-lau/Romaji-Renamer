# Baseline: bash write-metadata stage

Measured with `tests/golden/run_bash.sh --scenario <name>` on the real `functions.sh`
(repo HEAD `d43384f` + the working-tree `functions.sh`, sha256 prefix `8c7d7abde319`; the prefetch
functions that were being edited concurrently are not exercised by this stage).
Host: 4 CPUs, bash 5.1.16, jq 1.6, system awk (Ubuntu 22.04). `LC_ALL=C.utf8`.

| scenario | shows | wall (unstraced, idle box) | execve | clone (forks) |
|----------|-------|----------------------------|--------|---------------|
| `default` (animes, 20 shows) | 20 | **29.0 s** | **2371** | **4474** |
| `movies` (6 movies)          | 6  | **2.75 s** | 228    | 368 |

Under `strace -f` the same `default` run takes 38.8 s wall (user 28.2 s, sys 11.2 s).
A full `compare.sh -j 4` of all 21 scenarios takes about 4.5-5 min.

Commands used:

```
strace -f -c -e trace=execve,clone,clone3,fork,vfork -o OUT tests/golden/run_bash.sh --scenario default --output FILE
strace -f -e trace=execve -o OUT ...   # per-binary breakdown
```

`execve` breakdown for `default` (exact count 2371, almost all inside `$(...)` pipelines):

| binary | execs |
|--------|-------|
| jq | 762 |
| awk | 623 |
| date | 244 |
| stat | 157 |
| grep | 110 |
| sed | 100 |
| paste | 91 |
| tee | 85 |
| wc | 60 |
| du | 46 |
| sort / cut | 26 / 26 |
| head | 23 |
| tr | 8 |
| cat | 5 |

The Python port should make this number 0 (the harness's `strace -c -e trace=execve` on the port
should only show the interpreter start).
