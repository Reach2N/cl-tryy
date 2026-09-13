#!/usr/bin/env bash
set -e

# Always run from the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PID_FILE="$SCRIPT_DIR/.worker.pid"
LOG_FILE="$SCRIPT_DIR/worker.log"
DEFAULT_SUPABASE_URL="https://kshqavwsvsyrciuhauqo.supabase.co"
DEFAULT_SUPABASE_KEY="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImtzaHFhdndzdnN5cmNpdWhhdXFvIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODkzMDMwMTAsImV4cCI6MjEwNDg3OTAxMH0.nVBB-bqG08c0xCg_HiBjw3zCVEHqCFqPQVEOT0JjLbU"

# -------------------------------------------------------------
# Helper: Check and Install Environment & Dependencies
# -------------------------------------------------------------
setup_environment() {
    # 0. Check & install git if missing
    if ! command -v git &>/dev/null; then
        echo "📦 Installing git..."
        if command -v apt-get &>/dev/null; then
            sudo apt-get update -qq && sudo apt-get install -y -qq git
        fi
    fi

    # 1. Check Python 3
    if ! command -v python3 &>/dev/null; then
        echo "❌ Python 3 not found. Installing..."
        if command -v apt-get &>/dev/null; then
            sudo apt-get update -qq && sudo apt-get install -y -qq python3 python3-venv python3-pip
        else
            echo "Please install Python 3.10+ and re-run."
            exit 1
        fi
    fi

    # 2. Setup virtual environment
    if [ ! -d ".venv" ]; then
        echo "📦 Creating virtual environment (.venv)..."
        if ! python3 -m venv .venv 2>/dev/null; then
            if command -v apt-get &>/dev/null; then
                sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv
                python3 -m venv .venv
            else
                echo "Please install python3-venv and re-run."
                exit 1
            fi
        fi
    fi

    # 3. Install / Verify Dependencies
    if [ ! -f ".venv/.installed" ] || [ requirements.txt -nt .venv/.installed ]; then
        echo "🔄 Installing / verifying dependencies..."
        .venv/bin/pip install --upgrade pip -q
        .venv/bin/pip install -q -r requirements.txt
        touch .venv/.installed
    fi

    # 4. Check or configure .env automatically
    if [ ! -f ".env" ] || ! grep -q "SUPABASE_KEY" .env || grep -q 'SUPABASE_KEY=""' .env; then
        DEFAULT_WORKER="$(hostname | cut -d. -f1)-$(date +%s | tail -c 4)"
        cat <<EOF > .env
SUPABASE_URL="${DEFAULT_SUPABASE_URL}"
SUPABASE_KEY="${DEFAULT_SUPABASE_KEY}"
WORKER_ID="${DEFAULT_WORKER}"
BASE_URL="https://claude.ai/referral"
REQUEST_INTERVAL="2.2"
EOF
        echo "✅ Pre-configured .env with unique worker: ${DEFAULT_WORKER}"
    fi
}

# -------------------------------------------------------------
# Worker Control Functions
# -------------------------------------------------------------
is_running() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if ps -p "$PID" > /dev/null 2>&1; then
            return 0
        fi
    fi
    return 1
}

start_worker() {
    if is_running; then
        echo "⚠️ Worker is already running with PID $(cat "$PID_FILE")."
        show_logs
        exit 0
    fi

    setup_environment

    echo "🚀 Starting worker in background (immune to SSH disconnects)..."

    # Launch background loop with nohup: auto-restarts if it ever crashes
    nohup bash -c "
        while true; do
            '$SCRIPT_DIR/.venv/bin/python' -u '$SCRIPT_DIR/distributed_supabase.py' >> '$LOG_FILE' 2>&1
            EXIT_CODE=\$?
            if [ \$EXIT_CODE -eq 0 ]; then
                echo '[*] Worker finished cleanly.' >> '$LOG_FILE'
                break
            fi
            echo '[!] Worker exited with code '\$EXIT_CODE'. Auto-restarting in 5s...' >> '$LOG_FILE'
            sleep 5
        done
    " > /dev/null 2>&1 &

    WORKER_PID=$!
    echo $WORKER_PID > "$PID_FILE"
    echo "✅ Worker started! (PID: $WORKER_PID)"
    echo "📄 Logging to: $LOG_FILE"
    echo "💡 Note: You can close your terminal or SSH anytime — the worker will keep running!"
    echo "----------------------------------------------------------"
    echo "Streaming live logs (Press Ctrl + C to exit view without stopping worker):"
    echo "----------------------------------------------------------"
    sleep 1
    show_logs
}

stop_worker() {
    if is_running; then
        PID=$(cat "$PID_FILE")
        echo "🛑 Stopping worker (PID $PID)..."
        kill "$PID" 2>/dev/null || true
        # Also kill any python process spawned
        pkill -P "$PID" 2>/dev/null || true
        pkill -f "distributed_supabase.py" 2>/dev/null || true
        rm -f "$PID_FILE"
        echo "✅ Worker stopped."
    else
        echo "ℹ️ No running worker found."
        rm -f "$PID_FILE"
    fi
}

status_worker() {
    if is_running; then
        echo "🟢 Worker is RUNNING (PID: $(cat "$PID_FILE"))"
        echo "Recent log output:"
        tail -n 10 "$LOG_FILE" 2>/dev/null || true
    else
        echo "🔴 Worker is STOPPED."
    fi
}

show_logs() {
    if [ ! -f "$LOG_FILE" ]; then
        touch "$LOG_FILE"
    fi
    trap 'echo -e "\n👀 Detached from logs. Worker is still running in background!"; exit 0' INT
    tail -f "$LOG_FILE"
}

# -------------------------------------------------------------
# Main CLI Dispatch
# -------------------------------------------------------------
case "${1:-run}" in
    start|run)
        start_worker
        ;;
    stop)
        stop_worker
        ;;
    restart|update)
        echo "🔄 Pulling latest updates from GitHub..."
        git pull --quiet 2>/dev/null || true
        stop_worker
        sleep 1
        start_worker
        ;;
    status)
        status_worker
        ;;
    logs)
        show_logs
        ;;
    *)
        echo "Usage: ./run.sh [start|stop|restart|status|logs]"
        echo ""
        echo "  ./run.sh         -> Starts worker in background & streams live logs"
        echo "  ./run.sh stop    -> Stops the background worker"
        echo "  ./run.sh status  -> Checks if the worker is alive"
        echo "  ./run.sh logs    -> Views live log stream"
        exit 1
        ;;
esac
