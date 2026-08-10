# AWS Cloud FinOps Idle Resource Scanner (Lean MVP)

A lightweight, single-file CLI waste scanner for AWS accounts. Detects unattached storage, unassociated IP addresses, and idle compute/database instances, calculating estimated monthly USD waste.

## Features
- 🔍 **Unattached EBS Volumes:** Finds `available` volumes & estimates monthly storage cost.
- 🌐 **Unassociated Elastic IPs:** Finds idle public IPs costing $3.60/mo each.
- ⚡ **Idle EC2 Instances:** Scans CloudWatch CPU utilization over 7 days (<5% avg CPU).
- 🗄️ **Idle RDS Databases:** Scans CloudWatch DB connections over 7 days (0 active connections).
- 🔐 **Cross-Account Ready:** Supports AWS STS `AssumeRole` with `ExternalID`.
- 📊 **JSON & CLI Report:** Formatted ASCII table report or structured JSON output.

---

## Quickstart

### 1. Install Dependencies
```bash
pip install boto3
```

### 2. Run Local Account Waste Audit
```bash
# Basic scan (default: us-east-1)
python aws_waste_scanner.py

# Multi-region scan
python aws_waste_scanner.py --regions us-east-1 us-west-2 eu-west-1

# JSON output for API/automation integration
python aws_waste_scanner.py --json
```

### 3. Run Cross-Account Scan (For Client Audits)
```bash
python aws_waste_scanner.py --role-arn arn:aws:iam::123456789012:role/FinOpsScannerCrossAccountRole --external-id YOUR_UUID_HERE
```

---

## Files Created
- [`aws_waste_scanner.py`](file:///C:/Users/bhupendra_sirvi/.gemini/antigravity/scratch/cloud-finops-scanner/aws_waste_scanner.py): Main scanner script.
- [`template.yaml`](file:///C:/Users/bhupendra_sirvi/.gemini/antigravity/scratch/cloud-finops-scanner/template.yaml): 1-click AWS CloudFormation template for IAM cross-account access.
