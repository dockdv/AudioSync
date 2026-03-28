#!/bin/bash
cd "$(dirname "$0")"
echo "Starting Audio Sync & Merge -- Web Interface..."
echo ""
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi
source .venv/bin/activate
echo "Installing dependencies..."
pip3 install -r requirements.txt
echo ""
python3 app.py
