#!/usr/bin/env bash
# Avvia TuDirector su http://0.0.0.0:18736
cd "$(dirname "$0")"
exec python3 -m tuddirector.main
