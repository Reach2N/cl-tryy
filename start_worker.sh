#!/usr/bin/env bash
set -e

# Always run from the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DEFAULT_SUPABASE_URL="https://kshqavwsvsyrciuhauqo.supabase.co"

echo "=========================================================="
echo "    🚀 Supabase Distributed Worker Setup & Runner"
echo "=========================================================="

# 1. Check Python 3
if ! command -v python3 &>/dev/null; then
    echo "❌ Python 3 not found."
    if command -v apt-get &>/dev/null; then
        echo "Installing Python 3..."
        sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip
    else
        echo "Please install Python 3.10+ and re-run."
        exit 1
    fi
fi

# 2. Setup virtual environment
if [ ! -d ".venv" ]; then
    echo "📦 Creating virtual environment (.venv)..."
    if ! python3 -m venv .venv 2>/dev/null; then
        echo "⚠️ python3-venv module missing. Installing on Debian/Ubuntu..."
        if command -v apt-get &>/dev/null; then
            sudo apt-get update && sudo apt-get install -y python3-venv
            python3 -m venv .venv
        else
            echo "Please install python3-venv and re-run."
            exit 1
        fi
    fi
fi

# 3. Install / Verify Dependencies
echo "🔄 Checking & installing dependencies..."
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -q -r requirements.txt

# 4. Check or configure .env
if [ ! -f ".env" ] || ! grep -q "SUPABASE_KEY" .env || grep -q 'SUPABASE_KEY=""' .env; then
    echo ""
    echo "⚙️ First-time Supabase configuration:"
    echo "----------------------------------------------------------"
    echo "Your Project URL is: ${DEFAULT_SUPABASE_URL}"
    echo "Get your API key here: https://supabase.com/dashboard/project/kshqavwsvsyrciuhauqo/settings/api"
    echo ""
    read -rp "👉 Paste your Supabase Key (anon or service_role): " input_key
    while [ -z "$input_key" ]; do
        read -rp "Key cannot be empty. Paste your Supabase Key: " input_key
    done

    DEFAULT_WORKER="$(hostname)-$(date +%s | tail -c 4)"
    read -rp "Worker Name [${DEFAULT_WORKER}]: " input_worker
    input_worker=${input_worker:-$DEFAULT_WORKER}

    read -rp "Target URL [https://claude.ai/referral]: " input_base
    input_base=${input_base:-https://claude.ai/referral}

    cat <<EOF > .env
SUPABASE_URL="${DEFAULT_SUPABASE_URL}"
SUPABASE_KEY="${input_key}"
WORKER_ID="${input_worker}"
BASE_URL="${input_base}"
REQUEST_INTERVAL="2.0"
EOF

    echo "✅ Configuration saved to .env"
    echo ""
fi

# 5. Launch the worker
echo "✨ Starting worker process..."
exec .venv/bin/python -u distributed_supabase.py
