#!/usr/bin/env bash
#
# Setup a new Google account for the video generation pipeline.
# Creates project, enables APIs, creates service account, downloads key.
#
# Usage: bash setup_new_account.sh [email@gmail.com]

set -euo pipefail

EMAIL="${1:-}"

echo "============================================"
echo "  Video Pipeline - New Account Setup"
echo "============================================"

# Step 1: Auth
if [ -n "$EMAIL" ]; then
    echo ""
    echo "[1/7] Authenticating as $EMAIL..."
    gcloud auth login --account="$EMAIL" --brief
else
    echo ""
    echo "[1/7] Authenticating (browser will open)..."
    gcloud auth login --brief
fi

ACCOUNT=$(gcloud config get-value account 2>/dev/null)
echo "  Signed in as: $ACCOUNT"

# Step 2: Create project
PROJECT_ID="video-gen-$(date +%s | tail -c 8)"
echo ""
echo "[2/7] Creating project: $PROJECT_ID"
gcloud projects create "$PROJECT_ID" --name="Video Generator" --quiet
gcloud config set project "$PROJECT_ID" --quiet
echo "  Project created."

# Step 3: Link billing
echo ""
echo "[3/7] Linking billing account..."
BILLING_ACCOUNTS=$(gcloud billing accounts list --format="value(name)" 2>/dev/null || true)
BILLING_COUNT=$(echo "$BILLING_ACCOUNTS" | grep -c . || true)

if [ "$BILLING_COUNT" -eq 0 ]; then
    echo "  ERROR: No billing accounts found."
    echo "  Go to https://console.cloud.google.com/billing to create one."
    echo "  Then run: gcloud billing projects link $PROJECT_ID --billing-account=ACCOUNT_ID"
    exit 1
elif [ "$BILLING_COUNT" -eq 1 ]; then
    BILLING_ID="$BILLING_ACCOUNTS"
    echo "  Using billing account: $BILLING_ID"
else
    echo "  Available billing accounts:"
    gcloud billing accounts list
    echo ""
    read -rp "  Enter billing account ID: " BILLING_ID
fi

gcloud billing projects link "$PROJECT_ID" --billing-account="$BILLING_ID" --quiet
echo "  Billing linked."

# Step 4: Enable APIs
echo ""
echo "[4/7] Enabling APIs..."
for API in aiplatform.googleapis.com iam.googleapis.com cloudresourcemanager.googleapis.com; do
    echo "  Enabling $API..."
    gcloud services enable "$API" --project="$PROJECT_ID" --quiet
done
echo "  APIs enabled."

# Step 5: Create service account
SA_NAME="video-pipeline"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
echo ""
echo "[5/7] Creating service account: $SA_EMAIL"
gcloud iam service-accounts create "$SA_NAME" \
    --display-name="Video Pipeline SA" \
    --project="$PROJECT_ID" --quiet
echo "  Service account created."

# Step 6: Grant IAM role
echo ""
echo "[6/7] Granting aiplatform.user role..."
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/aiplatform.user" --quiet >/dev/null
echo "  Role granted."

# Step 7: Download key
KEY_FILE="${PROJECT_ID}-key.json"
echo ""
echo "[7/7] Downloading service account key..."
gcloud iam service-accounts keys create "$KEY_FILE" \
    --iam-account="$SA_EMAIL" \
    --project="$PROJECT_ID"
echo "  Key saved: $KEY_FILE"

# Summary
echo ""
echo "============================================"
echo "  Setup Complete"
echo "============================================"
echo ""
echo "  Project ID:   $PROJECT_ID"
echo "  SA Email:     $SA_EMAIL"
echo "  Key File:     $KEY_FILE"
echo ""
echo "  Update constants.py:"
echo "    GCP_PROJECT_ID = \"$PROJECT_ID\""
echo "    SERVICE_ACCOUNT_KEY_PATH = \"$KEY_FILE\""
echo ""
echo "  Test with:"
echo "    uv run python video_pipeline_v31.py --dry-run"
echo ""
echo "  Note: IAM propagation may take 1-2 minutes."
echo "============================================"
