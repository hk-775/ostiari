#!/usr/bin/env python3
"""CDK app for deploying Ostiari landing page to S3 + CloudFront."""
import aws_cdk as cdk

from stack import OstiariSiteStack

app = cdk.App()

OstiariSiteStack(
    app,
    "OstiariSite",
    env=cdk.Environment(
        account=cdk.Aws.ACCOUNT_ID,
        region="us-east-1",
    ),
)

app.synth()
