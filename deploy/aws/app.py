#!/usr/bin/env python3
"""CDK application for all Ostiari AWS deployment profiles."""

from aws_cdk import App, Environment
from stack import OstiariStack

from config import DeploymentConfig

config = DeploymentConfig.load()
app = App()
OstiariStack(
    app,
    config.stack_name,
    config=config,
    env=Environment(account=config.account, region=config.region),
    termination_protection=config.production,
    description=f"Ostiari deployment ({config.profile})",
)
app.synth()
