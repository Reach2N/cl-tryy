#!/usr/bin/env bash
set -e

echo "=========================================================="
echo "    🚀 Automated Cloud Worker Installer & Launcher"
echo "=========================================================="

PROXY_URL="$1"

# 1. Install git, curl, and python3 if missing
echo "📦 Checking system packages..."
if command -v apt-get &>/dev/null; then
    if ! command -v git &>/dev/null || ! command -v curl &>/dev/null || ! command -v python3 &>/dev/null; then
        echo "Installing git, curl, python3, and venv..."
        sudo apt-get update -y -qq
        sudo apt-get install -y -qq git curl python3 python3-venv python3-pip
    fi
elif command -v dnf &>/dev/null; then
    sudo dnf install -y -q git curl python3 python3-pip
elif command -v yum &>/dev/null; then
    sudo yum install -y -q git curl python3 python3-pip
elif command -v apk &>/dev/null; then
    apk add --no-cache git curl python3 py3-pip bash
fi

# 2. Clone or update the repository
INSTALL_DIR="$HOME/claude-link"
if [ -d "$INSTALL_DIR" ]; then
    echo "📁 Updating existing directory at $INSTALL_DIR..."
    cd "$INSTALL_DIR"
    git pull --quiet || true
else
    echo "📥 Cloning repository into $INSTALL_DIR..."
    git clone --quiet https://github.com/Reach2N/cl-tryy.git "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# 3. Download proxies if URL is provided
if [ -n "$PROXY_URL" ]; then
    echo "🌐 Downloading proxies from URL..."
    curl -s "$PROXY_URL" > proxies.txt
    if grep -q "{" proxies.txt; then
        echo "❌ Error: The proxy download link returned a JSON error (likely an expired or invalid token)."
        echo "Please generate a fresh Webshare download link and try again."
        rm proxies.txt
    else
        echo "✅ Proxies downloaded successfully to proxies.txt"
    fi
fi

# 4. Make run.sh executable and launch/restart
chmod +x run.sh
echo "🚀 Starting worker..."
exec ./run.sh restart
