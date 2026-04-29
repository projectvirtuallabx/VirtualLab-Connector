#!/bin/bash
# Run the Python connector service with environment variables from .env.connector

set -a
if [ -f .env.connector ]; then
  source .env.connector
fi
set +a

python3 connector.py --serve
