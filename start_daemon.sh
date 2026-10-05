#!/bin/bash
set -e
cd /home/patelram5002/paper_lab

echo "Stopping any existing live scanner..."
pkill -9 -f "main.py live" 2>/dev/null || true
sleep 2

echo "Starting fresh live scanner in background..."
nohup .venv/bin/python main.py live > logs/orb_scanner.out 2>&1 &
sleep 3

ps aux | grep "main.py live" | grep -v grep
echo "Live scanner started successfully!"
