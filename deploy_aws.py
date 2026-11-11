#!/usr/bin/env python3
"""
AWS 100% Serverless Deployment Script for mstool Dashboard
Deploys Flask Dashboard + DynamoDB + S3 Backend to AWS Lambda with Function URL (100% Free Tier / $0/mo).
Zero Supabase Dependency.
"""

import os
import sys
import json
import shutil
import zipfile
import subprocess
import time
import boto3
from dotenv import dotenv_values

def deploy():
    print("==================================================")
    print("   Deploying 100% AWS Serverless mstool Dashboard")
    print("==================================================")

    # 1. Load AWS Credentials
    env_file = ".EVN" if os.path.exists(".EVN") else ".env"
    config = dotenv_values(env_file)
    
    ak = config.get("AWS_ACCESS_KEY") or os.environ.get("AWS_ACCESS_KEY_ID")
    sk = config.get("AWS_SECRET_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY")
    reg = config.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "ap-south-1")

    if not ak or not sk:
        print("[Error] AWS credentials not found in .EVN or environment variables!")
        sys.exit(1)

    session = boto3.Session(aws_access_key_id=ak, aws_secret_access_key=sk, region_name=reg)
    account_id = session.client("sts").get_caller_identity()["Account"]
    
    bucket_name = config.get("S3_BUCKET", f"mstool-screenshots-{account_id}-{reg}")
    devices_table = config.get("DEVICES_TABLE", "mstool_devices")
    logs_table = config.get("LOGS_TABLE", "mstool_activity_logs")

    dist_dir = os.path.abspath("lambda_dist")
    zip_path = os.path.abspath("deployment.zip")

    # 2. Prepare build directory
    if os.path.exists(dist_dir):
        shutil.rmtree(dist_dir)
    os.makedirs(dist_dir, exist_ok=True)

    print("[1/5] Downloading Linux dependencies (Flask, python-dotenv, apig-wsgi)...")
    pkgs = ["Flask", "python-dotenv", "apig-wsgi"]
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--platform", "manylinux2014_x86_64",
        "--target", dist_dir,
        "--implementation", "cp",
        "--python-version", "3.12",
        "--only-binary=:all:",
        "--no-cache-dir"
    ] + pkgs
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"[Error] Pip failed: {res.stderr}")
        sys.exit(1)

    print("[2/5] Copying application files...")
    shutil.copy("app.py", os.path.join(dist_dir, "app.py"))
    shutil.copy("watchdog.py", os.path.join(dist_dir, "watchdog.py"))
    if os.path.exists("setup_client.ps1"):
        shutil.copy("setup_client.ps1", os.path.join(dist_dir, "setup_client.ps1"))

    handler_code = """import os
from apig_wsgi import make_lambda_handler
from app import app

handler = make_lambda_handler(app)
"""
    with open(os.path.join(dist_dir, "lambda_handler.py"), "w", encoding="utf-8") as f:
        f.write(handler_code)

    print("[3/5] Compressing deployment package...")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(dist_dir):
            for f in files:
                full_path = os.path.join(root, f)
                arcname = os.path.relpath(full_path, dist_dir)
                zf.write(full_path, arcname)

    print(f"      Package size: {os.path.getsize(zip_path) / (1024*1024):.2f} MB")

    # 3. AWS Client & IAM
    lam = session.client("lambda")
    iam = session.client("iam")
    function_name = "mstool-dashboard"
    role_name = "mstool-dashboard-role"

    print("[4/5] Verifying IAM role and permissions...")
    try:
        role = iam.get_role(RoleName=role_name)
        role_arn = role["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        assume_policy = {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]
        }
        role = iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=json.dumps(assume_policy))
        iam.attach_role_policy(RoleName=role_name, PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
        role_arn = role["Role"]["Arn"]
        time.sleep(10)

    # Attach DynamoDB and S3 access policy to the role
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:BatchWriteItem"
                ],
                "Resource": [
                    f"arn:aws:dynamodb:{reg}:{account_id}:table/{devices_table}",
                    f"arn:aws:dynamodb:{reg}:{account_id}:table/{logs_table}",
                    f"arn:aws:dynamodb:{reg}:{account_id}:table/{logs_table}/*"
                ]
            },
            {
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{bucket_name}", f"arn:aws:s3:::{bucket_name}/*"]
            }
        ]
    }
    iam.put_role_policy(RoleName=role_name, PolicyName="MSToolAccessPolicy", PolicyDocument=json.dumps(policy))

    print("[5/5] Deploying Lambda code & environment...")
    with open(zip_path, "rb") as f:
        zip_bytes = f.read()

    env_vars = {
        "SERVER_ONLY": "true",
        "S3_BUCKET": bucket_name,
        "DEVICES_TABLE": devices_table,
        "LOGS_TABLE": logs_table
    }

    try:
        lam.get_function(FunctionName=function_name)
        lam.update_function_code(FunctionName=function_name, ZipFile=zip_bytes)
        waiter = lam.get_waiter("function_updated")
        waiter.wait(FunctionName=function_name)
        lam.update_function_configuration(
            FunctionName=function_name,
            Runtime="python3.12",
            Handler="lambda_handler.handler",
            Timeout=30,
            MemorySize=256,
            Role=role_arn,
            Environment={"Variables": env_vars}
        )
    except lam.exceptions.ResourceNotFoundException:
        lam.create_function(
            FunctionName=function_name,
            Runtime="python3.12",
            Role=role_arn,
            Handler="lambda_handler.handler",
            Code={"ZipFile": zip_bytes},
            Timeout=30,
            MemorySize=256,
            Environment={"Variables": env_vars}
        )
        waiter = lam.get_waiter("function_active")
        waiter.wait(FunctionName=function_name)

    # Function URL config
    try:
        url_config = lam.get_function_url_config(FunctionName=function_name)
    except lam.exceptions.ResourceNotFoundException:
        url_config = lam.create_function_url_config(FunctionName=function_name, AuthType="NONE")
        lam.add_permission(
            FunctionName=function_name,
            StatementId="FunctionURLAllowPublicAccess",
            Action="lambda:InvokeFunctionUrl",
            Principal="*",
            FunctionUrlAuthType="NONE"
        )
        lam.add_permission(
            FunctionName=function_name,
            StatementId="FunctionAllowInvoke",
            Action="lambda:InvokeFunction",
            Principal="*"
        )

    # Clean up
    if os.path.exists(dist_dir):
        shutil.rmtree(dist_dir)
    if os.path.exists(zip_path):
        os.remove(zip_path)

    print("\n==================================================")
    print("   100% AWS DEPLOYMENT SUCCESSFUL!")
    print(f"   Dashboard Live URL: {url_config['FunctionUrl']}")
    print(f"   DynamoDB Devices:   {devices_table}")
    print(f"   DynamoDB Logs:      {logs_table}")
    print(f"   S3 Screenshot Bucket: {bucket_name}")
    print("==================================================\n")

if __name__ == "__main__":
    deploy()
