#!/bin/sh
# Init container for the llama.cpp servers: download MODEL_URL to /models/MODEL_FILE once, and
# check it against MODEL_SHA256. An existing file is re-checked on every start, so a corrupt or
# half-written file is downloaded again instead of being served.
set -eu
target="/models/$MODEL_FILE"
if [ -f "$target" ] && echo "$MODEL_SHA256  $target" | sha256sum --check --status; then
  echo "$MODEL_FILE is present and verified"
  exit 0
fi
echo "downloading $MODEL_FILE from $MODEL_URL"
curl --fail --location --retry 5 --retry-delay 10 --no-progress-meter \
  --output "$target.part" "$MODEL_URL"
if ! echo "$MODEL_SHA256  $target.part" | sha256sum --check; then
  rm -f "$target.part"
  exit 1
fi
mv "$target.part" "$target"
