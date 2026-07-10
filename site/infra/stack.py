"""CloudFront + S3 stack for ostiari.dev landing page."""
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Stack,
    RemovalPolicy,
    CfnOutput,
    aws_s3 as s3,
    aws_s3_deployment as s3_deploy,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_certificatemanager as acm,
    aws_route53 as route53,
    aws_route53_targets as targets,
)
from constructs import Construct

SITE_DIR = str(Path(__file__).parent.parent)


class OstiariSiteStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        domain_name = self.node.try_get_context("domain") or ""
        hosted_zone_id = self.node.try_get_context("hosted_zone_id") or ""

        bucket = s3.Bucket(
            self,
            "SiteBucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        )

        oai = cloudfront.OriginAccessIdentity(self, "OAI")
        bucket.grant_read(oai)

        certificate = None
        if domain_name and hosted_zone_id:
            zone = route53.HostedZone.from_hosted_zone_attributes(
                self, "Zone",
                hosted_zone_id=hosted_zone_id,
                zone_name=domain_name,
            )
            certificate = acm.Certificate(
                self, "Cert",
                domain_name=domain_name,
                validation=acm.CertificateValidation.from_dns(zone),
            )

        distribution = cloudfront.Distribution(
            self,
            "CDN",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3Origin(bucket, origin_access_identity=oai),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            ),
            default_root_object="index.html",
            domain_names=[domain_name] if domain_name else None,
            certificate=certificate,
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=cdk.Duration.seconds(0),
                ),
            ],
        )

        s3_deploy.BucketDeployment(
            self,
            "Deploy",
            sources=[s3_deploy.Source.asset(SITE_DIR, exclude=["infra", "infra/**"])],
            destination_bucket=bucket,
            distribution=distribution,
            distribution_paths=["/*"],
        )

        if domain_name and hosted_zone_id:
            route53.ARecord(
                self, "AliasRecord",
                zone=zone,
                target=route53.RecordTarget.from_alias(
                    targets.CloudFrontTarget(distribution)
                ),
                record_name=domain_name,
            )

        CfnOutput(self, "URL", value=f"https://{distribution.distribution_domain_name}")
        CfnOutput(self, "BucketName", value=bucket.bucket_name)
        CfnOutput(self, "DistributionId", value=distribution.distribution_id)
        if domain_name:
            CfnOutput(self, "DomainURL", value=f"https://{domain_name}")
