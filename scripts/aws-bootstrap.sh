#!/usr/bin/env bash
#
# Provision the AWS backend infra for the Cats API and deploy the first image.
# Idempotent — safe to re-run; existing resources are reused.
#
#   ECR repo -> build & push image -> S3 bucket -> IAM roles
#   -> RDS Postgres (in the default VPC) -> App Runner VPC connector
#   -> App Runner service (reaches RDS privately via the connector)
#
# Prereqs: aws CLI (`aws configure`), docker, python. Run from repo root. Fill
# scripts/prod.env first (cp scripts/prod.env.example scripts/prod.env) — set
# DATABASE_URL to your managed Postgres (e.g. Neon).
#
# DB default: external Postgres via DATABASE_URL (Neon free tier — $0, no VPC
# setup, standard Postgres so no lock-in). Set CREATE_RDS=true to instead
# auto-provision RDS + a VPC connector (adds ~5-10 min and ~$15/mo; the script
# then overwrites DATABASE_URL in prod.env with the generated one).
#
# COST: App Runner (~$5/mo + usage) + S3 (cents). CREATE_RDS=true adds RDS
# db.t4g.micro (~$12-15/mo).
#
# Still manual: custom domain (ACM cert + DNS) and CloudFront for media.
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

CREATE_RDS="${CREATE_RDS:-false}"   # default: use external DATABASE_URL (Neon). true => provision RDS.
DB_IDENTIFIER="cats-db"
DB_NAME="cats"
DB_USERNAME="cats"
DB_INSTANCE_CLASS="db.t4g.micro"
DB_STORAGE_GB="20"
DB_SUBNET_GROUP="cats-db-subnets"
SG_NAME="cats-db-sg"
VPC_CONNECTOR_NAME="cats-vpc-connector"
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

# ----- 5. RDS Postgres + networking --------------------------------------
CONNECTOR_ARN=""
if [ "$CREATE_RDS" = "true" ]; then
  say "Networking: default VPC, subnets, security group"
  VPC_ID="$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
    --query 'Vpcs[0].VpcId' --output text --region "$AWS_REGION")"
  [ "$VPC_ID" != "None" ] && [ -n "$VPC_ID" ] || {
    echo "no default VPC in $AWS_REGION — create one or set CREATE_RDS=false" >&2; exit 1; }
  SUBNET_IDS="$(aws ec2 describe-subnets --filters Name=vpc-id,Values="$VPC_ID" \
    --query 'Subnets[].SubnetId' --output text --region "$AWS_REGION" | tr '\t' ' ')"

  # One security group shared by RDS and the VPC connector, with a self-
  # referencing 5432 rule: the connector's ENIs (in this SG) reach RDS (in this
  # SG). RDS stays private (no public access).
  SG_ID="$(aws ec2 describe-security-groups \
    --filters Name=group-name,Values="$SG_NAME" Name=vpc-id,Values="$VPC_ID" \
    --query 'SecurityGroups[0].GroupId' --output text --region "$AWS_REGION" 2>/dev/null || echo None)"
  if [ "$SG_ID" = "None" ] || [ -z "$SG_ID" ]; then
    SG_ID="$(aws ec2 create-security-group --group-name "$SG_NAME" \
      --description "Cats DB + App Runner connector" --vpc-id "$VPC_ID" \
      --query GroupId --output text --region "$AWS_REGION")"
  fi
  aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
    --protocol tcp --port 5432 --source-group "$SG_ID" --region "$AWS_REGION" >/dev/null 2>&1 || true

  say "RDS Postgres: ${DB_IDENTIFIER} (this can take 5-10 min)"
  aws rds describe-db-subnet-groups --db-subnet-group-name "$DB_SUBNET_GROUP" --region "$AWS_REGION" >/dev/null 2>&1 \
    || aws rds create-db-subnet-group --db-subnet-group-name "$DB_SUBNET_GROUP" \
         --db-subnet-group-description "Cats DB" --subnet-ids $SUBNET_IDS --region "$AWS_REGION" >/dev/null

  if ! aws rds describe-db-instances --db-instance-identifier "$DB_IDENTIFIER" --region "$AWS_REGION" >/dev/null 2>&1; then
    DB_PASSWORD="$("$PYTHON" -c 'import secrets;print(secrets.token_urlsafe(18))')"
    aws rds create-db-instance \
      --db-instance-identifier "$DB_IDENTIFIER" \
      --db-instance-class "$DB_INSTANCE_CLASS" \
      --engine postgres \
      --master-username "$DB_USERNAME" \
      --master-user-password "$DB_PASSWORD" \
      --allocated-storage "$DB_STORAGE_GB" \
      --storage-type gp3 \
      --storage-encrypted \
      --db-name "$DB_NAME" \
      --vpc-security-group-ids "$SG_ID" \
      --db-subnet-group-name "$DB_SUBNET_GROUP" \
      --no-publicly-accessible \
      --no-multi-az \
      --backup-retention-period 7 \
      --region "$AWS_REGION" >/dev/null
    say "Waiting for RDS to become available..."
    aws rds wait db-instance-available --db-instance-identifier "$DB_IDENTIFIER" --region "$AWS_REGION"
    DB_ENDPOINT="$(aws rds describe-db-instances --db-instance-identifier "$DB_IDENTIFIER" \
      --query 'DBInstances[0].Endpoint.Address' --output text --region "$AWS_REGION")"
    DATABASE_URL="postgresql://${DB_USERNAME}:${DB_PASSWORD}@${DB_ENDPOINT}:5432/${DB_NAME}"
    # Persist the real URL into prod.env (gitignored) so App Runner gets it and
    # re-runs reuse it.
    DATABASE_URL="$DATABASE_URL" ENV_FILE="$ENV_FILE" "$PYTHON" - <<'PY'
import os
path, url = os.environ["ENV_FILE"], os.environ["DATABASE_URL"]
lines = open(path).read().splitlines()
out, found = [], False
for ln in lines:
    if ln.strip().startswith("DATABASE_URL="):
        out.append("DATABASE_URL=" + url); found = True
    else:
        out.append(ln)
if not found:
    out.append("DATABASE_URL=" + url)
open(path, "w").write("\n".join(out) + "\n")
PY
    say "DATABASE_URL written to ${ENV_FILE}"
  else
    say "RDS already exists — reusing DATABASE_URL from ${ENV_FILE}"
  fi

  say "App Runner VPC connector: ${VPC_CONNECTOR_NAME}"
  CONNECTOR_ARN="$(aws apprunner list-vpc-connectors --region "$AWS_REGION" \
    --query "VpcConnectors[?VpcConnectorName=='${VPC_CONNECTOR_NAME}' && Status=='ACTIVE'].VpcConnectorArn | [0]" \
    --output text 2>/dev/null || echo None)"
  if [ "$CONNECTOR_ARN" = "None" ] || [ -z "$CONNECTOR_ARN" ]; then
    CONNECTOR_ARN="$(aws apprunner create-vpc-connector --vpc-connector-name "$VPC_CONNECTOR_NAME" \
      --subnets $SUBNET_IDS --security-groups "$SG_ID" \
      --query VpcConnector.VpcConnectorArn --output text --region "$AWS_REGION")"
  fi
fi

# ----- 6. App Runner service ---------------------------------------------
# Build the create/update input JSON. Reads env vars from prod.env and, when a
# VPC connector exists, routes egress through it so RDS is reachable.
gen_input() {  # mode(create|update), arn-or-name
  MODE="$1" SVC="$2" IMAGE="$IMAGE" PORT=8000 ECR_ROLE_ARN="$ECR_ROLE_ARN" \
  INSTANCE_ROLE_ARN="$INSTANCE_ROLE_ARN" CPU="$CPU" MEMORY="$MEMORY" \
  ENV_FILE="$ENV_FILE" CONNECTOR_ARN="$CONNECTOR_ARN" "$PYTHON" - <<'PY'
import json, os
mode = os.environ["MODE"]
env = {}
with open(os.environ["ENV_FILE"]) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
doc = {
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
}
conn = os.environ.get("CONNECTOR_ARN", "")
if conn and conn != "None":
    doc["NetworkConfiguration"] = {
        "EgressConfiguration": {"EgressType": "VPC", "VpcConnectorArn": conn}
    }
if mode == "create":
    doc["ServiceName"] = os.environ["SVC"]
else:
    doc["ServiceArn"] = os.environ["SVC"]
print(json.dumps(doc))
PY
}

EXISTING_ARN="$(aws apprunner list-services --region "$AWS_REGION" \
  --query "ServiceSummaryList[?ServiceName=='${SERVICE_NAME}'].ServiceArn | [0]" --output text 2>/dev/null || echo None)"

# The native (Windows) aws CLI can't read git-bash /tmp paths, so write the
# input JSON into the repo dir and hand aws a file:// URI it understands.
INPUT="scripts/apprunner-input.json"
input_uri() { local p="$1"; command -v cygpath >/dev/null 2>&1 && p="$(cygpath -w "$p" | tr '\\' '/')"; printf 'file://%s' "$p"; }
trap 'rm -f "$INPUT"' EXIT

if [ "$EXISTING_ARN" != "None" ] && [ -n "$EXISTING_ARN" ]; then
  say "Updating existing App Runner service (env + network)"
  gen_input update "$EXISTING_ARN" > "$INPUT"
  aws apprunner update-service --region "$AWS_REGION" --cli-input-json "$(input_uri "$INPUT")" >/dev/null
  SERVICE_ARN="$EXISTING_ARN"
else
  say "Creating App Runner service: ${SERVICE_NAME}"
  gen_input create "$SERVICE_NAME" > "$INPUT"
  SERVICE_ARN="$(aws apprunner create-service --region "$AWS_REGION" \
    --cli-input-json "$(input_uri "$INPUT")" --query Service.ServiceArn --output text)"
fi
rm -f "$INPUT"

SERVICE_URL="$(aws apprunner describe-service --service-arn "$SERVICE_ARN" --region "$AWS_REGION" \
  --query Service.ServiceUrl --output text 2>/dev/null || echo '<pending>')"

# ----- done ---------------------------------------------------------------
say "Done."
cat <<EOF

  ECR image     : ${IMAGE}
  S3 bucket     : ${S3_BUCKET}
  RDS instance  : ${DB_IDENTIFIER} (DATABASE_URL in ${ENV_FILE})
  Service ARN   : ${SERVICE_ARN}
  Service URL   : https://${SERVICE_URL}

Next:
  1. Put the Service ARN into the GitHub secret APPRUNNER_SERVICE_ARN.
  2. Map api.catapp.uk to the service (ACM cert + custom domain).
  3. Migrate existing photos:
       aws s3 cp api/uploads/ s3://${S3_BUCKET}/uploads/ --recursive
EOF
