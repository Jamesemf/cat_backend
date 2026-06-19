#!/usr/bin/env bash
#
# Provision the stateless AWS backend infra for the Cats API and deploy the
# first image. Idempotent — safe to re-run; existing resources are reused.
#
#   ECR repo  ->  build & push image  ->  S3 bucket  ->  IAM roles  ->  App Runner
#
# Prereqs: aws CLI (logged in: `aws configure`), docker, python3. Run from repo
# root. Fill scripts/prod.env first (cp scripts/prod.env.example scripts/prod.env).
#
# NOT handled here (genuinely manual — networking/cert decisions):
#   * RDS Postgres + the App Runner VPC connector to reach it privately
#   * Custom domain api.cats.bytebrigade.net (ACM cert + DNS validation)
#   * CloudFront for media.cats.bytebrigade.net
# Until RDS is wired, set DATABASE_URL in prod.env to a reachable Postgres (or
# the service falls back to ephemeral SQLite — fine only for a smoke test).
set -euo pipefail

# ----- config -------------------------------------------------------------
AWS_REGION="${AWS_REGION:-us-east-1}"
ECR_REPO="cats-api"
S3_BUCKET="cats-media-prod"
SERVICE_NAME="cats-api"
ECR_ACCESS_ROLE="AppRunnerECRAccessRole"
INSTANCE_ROLE="CatsApiInstanceRole"
CPU="1 vCPU"
MEMORY="2 GB"
ENV_FILE="scripts/prod.env"
# --------------------------------------------------------------------------

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

for bin in aws docker; do
  command -v "$bin" >/dev/null || { echo "missing required tool: $bin" >&2; exit 1; }
done
# Pick a python that actually runs (the Windows 'python3' Store stub does not).
PYTHON=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys' >/dev/null 2>&1; then PYTHON="$c"; break; fi
done
[ -n "$PYTHON" ] || { echo "no working python found (need python3 or python)" >&2; exit 1; }
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (cp scripts/prod.env.example $ENV_FILE)" >&2; exit 1; }
[ -f api/Dockerfile ] || { echo "run from repo root (api/Dockerfile not found)" >&2; exit 1; }

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE="${REGISTRY}/${ECR_REPO}:latest"
say "Account ${ACCOUNT} / region ${AWS_REGION}"

# ----- 1. ECR repo --------------------------------------------------------
say "ECR repository: ${ECR_REPO}"
aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$AWS_REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$ECR_REPO" --region "$AWS_REGION" >/dev/null

# ----- 2. build & push first image ---------------------------------------
say "Build & push image -> ${IMAGE}"
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
docker build -f api/Dockerfile -t "$IMAGE" .
docker push "$IMAGE"

# ----- 3. S3 bucket -------------------------------------------------------
say "S3 bucket: ${S3_BUCKET}"
if ! aws s3api head-bucket --bucket "$S3_BUCKET" 2>/dev/null; then
  if [ "$AWS_REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$S3_BUCKET" --region "$AWS_REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$S3_BUCKET" --region "$AWS_REGION" \
      --create-bucket-configuration LocationConstraint="$AWS_REGION" >/dev/null
  fi
fi

# ----- 4. IAM roles -------------------------------------------------------
ensure_role() {  # name, service-principal
  aws iam get-role --role-name "$1" >/dev/null 2>&1 && return 0
  local trust
  trust="$(printf '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"%s"},"Action":"sts:AssumeRole"}]}' "$2")"
  aws iam create-role --role-name "$1" --assume-role-policy-document "$trust" >/dev/null
}

say "IAM: ECR access role ${ECR_ACCESS_ROLE}"
ensure_role "$ECR_ACCESS_ROLE" "build.apprunner.amazonaws.com"
aws iam attach-role-policy --role-name "$ECR_ACCESS_ROLE" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess >/dev/null 2>&1 || true
ECR_ROLE_ARN="$(aws iam get-role --role-name "$ECR_ACCESS_ROLE" --query Role.Arn --output text)"

say "IAM: instance role ${INSTANCE_ROLE} (S3 access)"
ensure_role "$INSTANCE_ROLE" "tasks.apprunner.amazonaws.com"
S3_POLICY="$(printf '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:ListBucket"],"Resource":["arn:aws:s3:::%s","arn:aws:s3:::%s/*"]}]}' "$S3_BUCKET" "$S3_BUCKET")"
aws iam put-role-policy --role-name "$INSTANCE_ROLE" --policy-name cats-s3-access \
  --policy-document "$S3_POLICY" >/dev/null
INSTANCE_ROLE_ARN="$(aws iam get-role --role-name "$INSTANCE_ROLE" --query Role.Arn --output text)"

# ----- 5. App Runner service ---------------------------------------------
EXISTING_ARN="$(aws apprunner list-services --region "$AWS_REGION" \
  --query "ServiceSummaryList[?ServiceName=='${SERVICE_NAME}'].ServiceArn | [0]" --output text 2>/dev/null || echo None)"

if [ "$EXISTING_ARN" != "None" ] && [ -n "$EXISTING_ARN" ]; then
  say "App Runner service already exists — pushing image triggers auto-deploy"
  aws apprunner start-deployment --service-arn "$EXISTING_ARN" --region "$AWS_REGION" >/dev/null || true
  SERVICE_ARN="$EXISTING_ARN"
else
  say "Creating App Runner service: ${SERVICE_NAME}"
  INPUT="$(mktemp)"
  IMAGE="$IMAGE" PORT=8000 ECR_ROLE_ARN="$ECR_ROLE_ARN" INSTANCE_ROLE_ARN="$INSTANCE_ROLE_ARN" \
  SERVICE_NAME="$SERVICE_NAME" CPU="$CPU" MEMORY="$MEMORY" ENV_FILE="$ENV_FILE" \
  "$PYTHON" - <<'PY' > "$INPUT"
import json, os
env = {}
with open(os.environ["ENV_FILE"]) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
print(json.dumps({
    "ServiceName": os.environ["SERVICE_NAME"],
    "SourceConfiguration": {
        "ImageRepository": {
            "ImageIdentifier": os.environ["IMAGE"],
            "ImageRepositoryType": "ECR",
            "ImageConfiguration": {
                "Port": os.environ["PORT"],
                "RuntimeEnvironmentVariables": env,
            },
        },
        "AutoDeploymentsEnabled": True,
        "AuthenticationConfiguration": {"AccessRoleArn": os.environ["ECR_ROLE_ARN"]},
    },
    "InstanceConfiguration": {
        "Cpu": os.environ["CPU"],
        "Memory": os.environ["MEMORY"],
        "InstanceRoleArn": os.environ["INSTANCE_ROLE_ARN"],
    },
    "HealthCheckConfiguration": {
        "Protocol": "HTTP", "Path": "/health",
        "Interval": 10, "Timeout": 5, "HealthyThreshold": 1, "UnhealthyThreshold": 5,
    },
}))
PY
  SERVICE_ARN="$(aws apprunner create-service --region "$AWS_REGION" \
    --cli-input-json "file://$INPUT" --query Service.ServiceArn --output text)"
  rm -f "$INPUT"
fi

SERVICE_URL="$(aws apprunner describe-service --service-arn "$SERVICE_ARN" --region "$AWS_REGION" \
  --query Service.ServiceUrl --output text 2>/dev/null || echo '<pending>')"

# ----- done ---------------------------------------------------------------
say "Done."
cat <<EOF

  ECR image     : ${IMAGE}
  S3 bucket     : ${S3_BUCKET}
  Service ARN   : ${SERVICE_ARN}
  Service URL   : https://${SERVICE_URL}

Next:
  1. Put the Service ARN into the GitHub secret APPRUNNER_SERVICE_ARN.
  2. Wire RDS + a VPC connector, then set DATABASE_URL on the service.
  3. Map api.cats.bytebrigade.net to the service (ACM cert + custom domain).
  4. Migrate existing photos:
       aws s3 cp api/uploads/ s3://${S3_BUCKET}/uploads/ --recursive
EOF
