#!/usr/bin/env python3
"""
AWS Cloud FinOps Idle Resource & Waste Scanner
Single-file, dependency-light CLI tool for detecting wasted AWS spend.

Ponytail Note:
- Pricing is calculated using standard US-East-1 baseline averages rather than calling the AWS Price List API.
  Upgrade path: Query AWS Price List API dynamically per region/instance-type.
- Single-threaded regional sweep by default.
  Upgrade path: Use concurrent.futures for parallel multi-region scanning.
"""

import argparse
import json
import itertools
from datetime import datetime, timedelta
import boto3
from botocore.exceptions import ClientError, NoCredentialsError

# Baseline estimated pricing constants (USD)
EBS_GP3_PRICE_PER_GB_MONTH = 0.08
EIP_IDLE_PRICE_PER_MONTH = 3.60  # $0.005 / hour

DEFAULT_EC2_MONTHLY_ESTIMATE = 70.00  # Baseline average instance cost
DEFAULT_RDS_MONTHLY_ESTIMATE = 150.00  # Baseline average database cost


def get_aws_session(role_arn=None, external_id=None, region="us-east-1", aws_creds=None):
    """Returns a boto3 session. Priority: aws_creds > STS AssumeRole > env/CLI config."""
    if aws_creds:
        return boto3.Session(
            aws_access_key_id=aws_creds.access_key,
            aws_secret_access_key=aws_creds.secret_key,
            region_name=aws_creds.region,
        )
    if not role_arn:
        return boto3.Session(region_name=region)

    sts_client = boto3.client("sts", region_name=region)
    assume_role_kwargs = {
        "RoleArn": role_arn,
        "RoleSessionName": "FinOpsWasteScannerSession"
    }
    if external_id:
        assume_role_kwargs["ExternalId"] = external_id

    response = sts_client.assume_role(**assume_role_kwargs)
    creds = response["Credentials"]

    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region
    )


def scan_unattached_ebs(session, region):
    """Finds EBS volumes with status 'available' (unattached)."""
    ec2 = session.client("ec2", region_name=region)
    waste_items = []

    try:
        response = ec2.describe_volumes(Filters=[{"Name": "status", "Values": ["available"]}])
        for vol in response.get("Volumes", []):
            size_gb = vol.get("Size", 0)
            vol_type = vol.get("VolumeType", "gp3")
            monthly_cost = round(size_gb * EBS_GP3_PRICE_PER_GB_MONTH, 2)

            waste_items.append({
                "category": "Unattached EBS Volume",
                "resource_id": vol["VolumeId"],
                "region": region,
                "details": f"{size_gb} GB ({vol_type})",
                "monthly_waste_usd": monthly_cost
            })
    except ClientError as e:
        print(f"[Warning] Failed scanning EBS in {region}: {e}", file=sys.stderr)

    return waste_items


def scan_unassociated_eips(session, region):
    """Finds Elastic IPs allocated but not associated with an instance/interface."""
    ec2 = session.client("ec2", region_name=region)
    waste_items = []

    try:
        response = ec2.describe_addresses()
        for addr in response.get("Addresses", []):
            if "AssociationId" not in addr:
                waste_items.append({
                    "category": "Unassociated Elastic IP",
                    "resource_id": addr.get("AllocationId", addr.get("PublicIp")),
                    "region": region,
                    "details": f"Public IP: {addr.get('PublicIp')}",
                    "monthly_waste_usd": EIP_IDLE_PRICE_PER_MONTH
                })
    except ClientError as e:
        print(f"[Warning] Failed scanning EIPs in {region}: {e}", file=sys.stderr)

    return waste_items


def scan_idle_ec2(session, region, days=7, cpu_threshold=5.0):
    """Finds running EC2 instances with average CPU utilization below threshold."""
    ec2 = session.client("ec2", region_name=region)
    cw = session.client("cloudwatch", region_name=region)
    waste_items = []

    start_time = datetime.utcnow() - timedelta(days=days)
    end_time = datetime.utcnow()

    try:
        response = ec2.describe_instances(Filters=[{"Name": "instance-state-name", "Values": ["running"]}])
        for reservation in response.get("Reservations", []):
            for inst in reservation.get("Instances", []):
                instance_id = inst["InstanceId"]
                instance_type = inst.get("InstanceType", "unknown")

                # CloudWatch CPU metrics query
                stats = cw.get_metric_statistics(
                    Namespace="AWS/EC2",
                    MetricName="CPUUtilization",
                    Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                    StartTime=start_time,
                    EndTime=end_time,
                    Period=86400,
                    Statistics=["Average"]
                )

                datapoints = stats.get("Datapoints", [])
                if datapoints:
                    avg_cpu = sum(d["Average"] for d in datapoints) / len(datapoints)
                    if avg_cpu < cpu_threshold:
                        waste_items.append({
                            "category": "Idle EC2 Instance",
                            "resource_id": instance_id,
                            "region": region,
                            "details": f"Type: {instance_type} | Avg CPU (7d): {avg_cpu:.1f}%",
                            "monthly_waste_usd": DEFAULT_EC2_MONTHLY_ESTIMATE
                        })
    except ClientError as e:
        print(f"[Warning] Failed scanning EC2 in {region}: {e}", file=sys.stderr)

    return waste_items


def scan_idle_rds(session, region, days=7):
    """Finds RDS instances with zero database connections over the last N days."""
    rds = session.client("rds", region_name=region)
    cw = session.client("cloudwatch", region_name=region)
    waste_items = []

    start_time = datetime.utcnow() - timedelta(days=days)
    end_time = datetime.utcnow()

    try:
        response = rds.describe_db_instances()
        for db in response.get("DBInstances", []):
            db_id = db["DBInstanceIdentifier"]
            db_class = db.get("DBInstanceClass", "db.t3.medium")

            stats = cw.get_metric_statistics(
                Namespace="AWS/RDS",
                MetricName="DatabaseConnections",
                Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db_id}],
                StartTime=start_time,
                EndTime=end_time,
                Period=86400,
                Statistics=["Sum"]
            )

            datapoints = stats.get("Datapoints", [])
            if datapoints:
                total_conns = sum(d["Sum"] for d in datapoints)
                if total_conns == 0:
                    waste_items.append({
                        "category": "Idle RDS Database",
                        "resource_id": db_id,
                        "region": region,
                        "details": f"Class: {db_class} | Connections (7d): 0",
                        "monthly_waste_usd": DEFAULT_RDS_MONTHLY_ESTIMATE
                    })
    except ClientError as e:
        print(f"[Warning] Failed scanning RDS in {region}: {e}")

    return waste_items


# ponytail: registry — add new scanners here, run_scanner picks them up automatically.
_SCANNERS = [scan_unattached_ebs, scan_unassociated_eips, scan_idle_ec2, scan_idle_rds]


def run_scanner(regions, role_arn=None, external_id=None, days=7, aws_creds=None):
    print("[SCAN] Starting AWS FinOps Waste Audit...\n")
    all_findings = []
    for region in regions:
        print(f"[SCAN] Scanning region: {region}...")
        try:
            session = get_aws_session(role_arn, external_id, region, aws_creds=aws_creds)
            all_findings.extend(itertools.chain.from_iterable(
                fn(session, region) if fn not in (scan_idle_ec2, scan_idle_rds)
                else fn(session, region, days=days)
                for fn in _SCANNERS
            ))
        except NoCredentialsError:
            print("[ERROR] No AWS credentials found. Please configure AWS credentials.")
            raise
        except Exception as e:
            print(f"[ERROR] Error scanning {region}: {e}")
            raise
    return all_findings


def print_cli_report(findings):
    print("\n" + "=" * 70)
    print("               AWS CLOUD FINOPS WASTE AUDIT REPORT               ")
    print("=" * 70)

    if not findings:
        print("[SUCCESS] Great news! No idle resources or wasted storage detected.")
        print("=" * 70 + "\n")
        return

    total_waste = sum(item["monthly_waste_usd"] for item in findings)

    print(f"{'CATEGORY':<25} {'RESOURCE ID':<22} {'REGION':<10} {'EST. MONTHLY WASTE'}")
    print("-" * 70)

    for item in findings:
        print(f"{item['category']:<25} {item['resource_id']:<22} {item['region']:<10} ${item['monthly_waste_usd']:>8.2f}")
        print(f"  |-- Details: {item['details']}")

    print("-" * 70)
    print(f"TOTAL ESTIMATED MONTHLY WASTED SPEND: ${total_waste:,.2f} / month")
    print("=" * 70 + "\n")


def main():
    parser = argparse.ArgumentParser(description="AWS FinOps Waste & Idle Resource Scanner")
    parser.add_argument("--regions", nargs="+", default=["us-east-1"], help="AWS regions to scan (default: us-east-1)")
    parser.add_argument("--role-arn", help="Optional IAM Role ARN for cross-account scan")
    parser.add_argument("--external-id", help="Optional External ID for IAM AssumeRole")
    parser.add_argument("--days", type=int, default=7, help="Days of CloudWatch history to analyze (default: 7)")
    parser.add_argument("--json", action="store_true", help="Output findings in JSON format")

    args = parser.parse_args()

    findings = run_scanner(
        regions=args.regions,
        role_arn=args.role_arn,
        external_id=args.external_id,
        days=args.days
    )

    if args.json:
        print(json.dumps(findings, indent=2))
    else:
        print_cli_report(findings)


if __name__ == "__main__":
    main()
