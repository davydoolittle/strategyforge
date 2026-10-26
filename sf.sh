#!/bin/sh
# StrategyForge research CLI runner.
# Runs any app/*.py from the LIVE share against the LIVE database in a
# throwaway container. No image rebuild, no container recreate, and the
# collector container keeps running untouched.
#
#   /mnt/user/appdata/strategyforge/sf.sh adversary.py --candidate 1033
#   /mnt/user/appdata/strategyforge/sf.sh lineage.py tree
#   /mnt/user/appdata/strategyforge/sf.sh pine_export.py --candidate 1033 --out /data/exports
#   /mnt/user/appdata/strategyforge/sf.sh -m unittest discover -s /src/tests
#   /mnt/user/appdata/strategyforge/sf.sh paper.py     # davesbridge signals.db, read-only at /bridge
#   /mnt/user/appdata/strategyforge/sf.sh health.py --ntfy-topic <topic>   # decay monitor
#
# The collector (main.py) is the only module that needs to be in the image;
# everything else is a one-shot CLI and belongs here.
ROOT=/mnt/user/appdata/strategyforge
exec docker run --rm -it \
  -v "$ROOT/app:/app" \
  -v "$ROOT/data:/data" \
  -v "$ROOT:/src" \
  -v /mnt/user/appdata/davesbridge/data:/bridge:ro \
  -w /app \
  python:3.12-slim python "$@"
