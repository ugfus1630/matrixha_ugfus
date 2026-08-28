#!/usr/bin/env bash
set -e

cd /opt/matrix_bridge

# The application reads /data/options.json directly rather than having run.sh
# export several dozen bashio variables.
exec python3 -m app.main
