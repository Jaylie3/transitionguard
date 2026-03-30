#!/bin/bash

# TransitionGuard Setup Script
# Initializes the project environment and runs initial tests

set -e

echo "═══════════════════════════════════════════════════════════════"
echo "TransitionGuard Setup Script"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# Check Python version
echo "✓ Checking Python version..."
python --version
echo ""

# Create virtual environment
echo "✓ Creating virtual environment..."
if [ ! -d "venv" ]; then
    python -m venv venv
    echo "  Virtual environment created"
else
    echo "  Virtual environment already exists"
fi
echo ""

# Activate virtual environment
echo "✓ Activating virtual environment..."
source venv/bin/activate
echo ""

# Install dependencies
echo "✓ Installing dependencies..."
pip install -q -r requirements.txt
echo "  Dependencies installed"
echo ""

# Run unit tests
echo "✓ Running unit tests..."
python -m pytest test_mcp_server.py -v --tb=short
echo ""

# Start MCP server
echo "═══════════════════════════════════════════════════════════════"
echo "Setup complete! Starting MCP server..."
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "TransitionGuard MCP Server will be available at:"
echo "  http://localhost:8000"
echo ""
echo "Test the LACE+ calculator:"
echo "  curl -X POST http://localhost:8000/tools/lace_plus_calculator -H 'Content-Type: application/json' -d '{\"age\": 72, \"length_of_stay_days\": 5, \"charlson_score\": 2, \"ed_visits_6mo\": 2, \"er_visits_past\": 1}'"
echo ""

python mcp_server.py
