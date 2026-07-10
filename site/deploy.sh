#!/usr/bin/env bash
set -euo pipefail

# Deploy Ostiari landing page to S3 + CloudFront
#
# Usage:
#   ./deploy.sh                          # Deploy without custom domain (CloudFront URL only)
#   ./deploy.sh ostiari.dev Z1234...  # Deploy with custom domain + Route53

DOMAIN="${1:-}"
ZONE_ID="${2:-}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$SCRIPT_DIR/infra"

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -q -r requirements.txt
else
    source .venv/bin/activate
fi

CONTEXT_ARGS=""
if [ -n "$DOMAIN" ] && [ -n "$ZONE_ID" ]; then
    CONTEXT_ARGS="-c domain=$DOMAIN -c hosted_zone_id=$ZONE_ID"
    echo "Deploying with custom domain: $DOMAIN"
else
    echo "Deploying without custom domain (CloudFront URL only)"
fi

echo "Synthesizing..."
cdk synth $CONTEXT_ARGS --quiet

echo "Deploying..."
cdk deploy $CONTEXT_ARGS --require-approval never

echo ""
echo "Done! Site is live."
