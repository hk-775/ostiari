"""AWS Lambda handler for Ostiari Gateway using Mangum."""

from mangum import Mangum

from ostiari_gateway.app import app

handler = Mangum(app, lifespan="off")
