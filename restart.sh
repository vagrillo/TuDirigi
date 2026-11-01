#!/usr/bin/env bash
# Riavvia TuDirector: ferma l'istanza attiva e riparte staccata dal terminale.
cd "$(dirname "$0")"

echo "→ fermo l'istanza attiva…"
pkill -f "python3 -m tuddirector.main" 2>/dev/null && sleep 1 || echo "  (nessuna istanza attiva)"

echo "→ avvio (log: data/server.log)…"
mkdir -p data
nohup ./run.sh > data/server.log 2>&1 &
echo "  PID: $!"

# attende che risponda
for i in $(seq 1 20); do
  sleep 0.5
  if curl -s -m 1 http://localhost:18736/api/health >/dev/null 2>&1; then
    echo "✓ TuDirector attivo su http://0.0.0.0:18736"
    exit 0
  fi
done
echo "✗ non risponde — controlla data/server.log"
exit 1
