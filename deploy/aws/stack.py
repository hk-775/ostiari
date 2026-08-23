"""Production-aware AWS deployment for the complete Ostiari platform."""

from __future__ import annotations

import re
from pathlib import Path

from aws_cdk import (
    ArnFormat,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
)
from aws_cdk import (
    aws_bedrockagentcore as agentcore,
)
from aws_cdk import (
    aws_certificatemanager as acm,
)
from aws_cdk import (
    aws_cloudwatch as cloudwatch,
)
from aws_cdk import (
    aws_cloudwatch_actions as cloudwatch_actions,
)
from aws_cdk import (
    aws_ec2 as ec2,
)
from aws_cdk import (
    aws_ecr as ecr,
)
from aws_cdk import (
    aws_ecr_assets as ecr_assets,
)
from aws_cdk import (
    aws_ecs as ecs,
)
from aws_cdk import (
    aws_elasticache as elasticache,
)
from aws_cdk import (
    aws_elasticloadbalancingv2 as elbv2,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_rds as rds,
)
from aws_cdk import (
    aws_route53 as route53,
)
from aws_cdk import (
    aws_route53_targets as route53_targets,
)
from aws_cdk import (
    aws_s3 as s3,
)
from aws_cdk import (
    aws_secretsmanager as secretsmanager,
)
from aws_cdk import (
    aws_servicediscovery as servicediscovery,
)
from aws_cdk import (
    aws_sns as sns,
)
from aws_cdk import (
    aws_wafv2 as wafv2,
)
from constructs import Construct

from config import DeploymentConfig

ROOT = Path(__file__).resolve().parents[2]


class OstiariStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: DeploymentConfig,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.config = config

        Tags.of(self).add("Application", "Ostiari")
        Tags.of(self).add("Profile", config.profile)
        Tags.of(self).add("ManagedBy", "OstiariDeploymentCLI")

        self._network()
        self._state()
        self._secrets()
        self._services()
        self._load_balancer()
        if config.agentcore:
            self._agentcore()
        if config.production:
            self._production_controls()
        self._outputs()

    def _network(self) -> None:
        needs_egress_subnets = self.config.production or self.config.agentcore
        subnets = [
            ec2.SubnetConfiguration(
                name="public",
                subnet_type=ec2.SubnetType.PUBLIC,
                cidr_mask=24,
            ),
            ec2.SubnetConfiguration(
                name="data",
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                cidr_mask=24,
            ),
        ]
        if needs_egress_subnets:
            subnets.insert(
                1,
                ec2.SubnetConfiguration(
                    name="application",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            )
        placement = (
            {"availability_zones": list(self.config.availability_zones)}
            if self.config.availability_zones
            else {"max_azs": 2}
        )
        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            nat_gateways=2 if self.config.production else (1 if needs_egress_subnets else 0),
            subnet_configuration=subnets,
            restrict_default_security_group=True,
            **placement,
        )
        self.app_subnets = ec2.SubnetSelection(
            subnet_type=(
                ec2.SubnetType.PRIVATE_WITH_EGRESS
                if needs_egress_subnets
                else ec2.SubnetType.PUBLIC
            )
        )
        self.assign_public_ip = not needs_egress_subnets

        self.alb_sg = ec2.SecurityGroup(
            self, "LoadBalancerSecurityGroup", vpc=self.vpc, allow_all_outbound=True
        )
        for index, cidr in enumerate(self.config.allowed_cidrs):
            peer = ec2.Peer.ipv6(cidr) if ":" in cidr else ec2.Peer.ipv4(cidr)
            if self.config.production:
                self.alb_sg.add_ingress_rule(peer, ec2.Port.tcp(443), f"HTTPS access {index + 1}")
                self.alb_sg.add_ingress_rule(peer, ec2.Port.tcp(80), f"HTTPS redirect {index + 1}")
            else:
                self.alb_sg.add_ingress_rule(
                    peer, ec2.Port.tcp(80), f"Dashboard access {index + 1}"
                )
                self.alb_sg.add_ingress_rule(
                    peer, ec2.Port.tcp(8421), f"Gateway access {index + 1}"
                )

        self.backend_sg = ec2.SecurityGroup(
            self, "ControlPlaneSecurityGroup", vpc=self.vpc, allow_all_outbound=True
        )
        self.gateway_sg = ec2.SecurityGroup(
            self, "GatewaySecurityGroup", vpc=self.vpc, allow_all_outbound=True
        )
        self.frontend_sg = ec2.SecurityGroup(
            self, "FrontendSecurityGroup", vpc=self.vpc, allow_all_outbound=True
        )
        self.demo_sg: ec2.SecurityGroup | None = None
        if self.config.demo:
            self.demo_sg = ec2.SecurityGroup(
                self, "DemoToolsSecurityGroup", vpc=self.vpc, allow_all_outbound=True
            )
            self.demo_sg.add_ingress_rule(
                self.gateway_sg,
                ec2.Port.tcp(9300),
            )

        self.backend_sg.add_ingress_rule(self.alb_sg, ec2.Port.tcp(8400))
        self.backend_sg.add_ingress_rule(self.gateway_sg, ec2.Port.tcp(8400))
        self.gateway_sg.add_ingress_rule(self.alb_sg, ec2.Port.tcp(8421))
        self.gateway_sg.add_ingress_rule(self.backend_sg, ec2.Port.tcp(8421))
        self.frontend_sg.add_ingress_rule(self.alb_sg, ec2.Port.tcp(9000))

        self.namespace = servicediscovery.PrivateDnsNamespace(
            self,
            "ServiceNamespace",
            name=self.config.namespace,
            vpc=self.vpc,
            description="Ostiari private service discovery",
        )

    def _state(self) -> None:
        db_removal = RemovalPolicy.SNAPSHOT if self.config.production else RemovalPolicy.DESTROY
        self.database = rds.DatabaseInstance(
            self,
            "Database",
            engine=rds.DatabaseInstanceEngine.postgres(version=rds.PostgresEngineVersion.VER_16_13),
            credentials=rds.Credentials.from_generated_secret(
                "ostiari",
                exclude_characters="\"'@/\\ ",
                secret_name=f"ostiari/{self.config.name}/database",
            ),
            database_name="ostiari",
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE4_GRAVITON,
                ec2.InstanceSize.SMALL if self.config.production else ec2.InstanceSize.MICRO,
            ),
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            allocated_storage=20,
            max_allocated_storage=200 if self.config.production else 100,
            storage_encrypted=True,
            multi_az=self.config.production,
            publicly_accessible=False,
            backup_retention=Duration.days(7 if self.config.production else 1),
            deletion_protection=self.config.production,
            auto_minor_version_upgrade=True,
            storage_type=rds.StorageType.GP3,
            enable_performance_insights=self.config.production,
            cloudwatch_logs_exports=["postgresql"],
            cloudwatch_logs_retention=(
                logs.RetentionDays.THREE_MONTHS
                if self.config.production
                else logs.RetentionDays.ONE_WEEK
            ),
            removal_policy=db_removal,
            delete_automated_backups=not self.config.production,
        )
        if self.database.secret is None:
            raise ValueError("RDS did not create a credentials secret")
        self.database.connections.allow_default_port_from(self.backend_sg)

        self.cache_sg = ec2.SecurityGroup(
            self, "ValkeySecurityGroup", vpc=self.vpc, allow_all_outbound=False
        )
        self.cache_sg.add_ingress_rule(self.backend_sg, ec2.Port.tcp(6379))
        self.cache_sg.add_ingress_rule(self.gateway_sg, ec2.Port.tcp(6379))
        data_subnets = self.vpc.select_subnets(
            subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
        ).subnet_ids
        cache_name = re.sub(r"[^a-z0-9-]", "-", f"ostiari-{self.config.name}".lower())[:40]
        self.cache = elasticache.CfnServerlessCache(
            self,
            "Valkey",
            engine="valkey",
            major_engine_version="8",
            serverless_cache_name=cache_name,
            description="Ostiari coordination, event fan-out, and rate limits",
            subnet_ids=data_subnets,
            security_group_ids=[self.cache_sg.security_group_id],
            snapshot_retention_limit=7 if self.config.production else 1,
            cache_usage_limits=elasticache.CfnServerlessCache.CacheUsageLimitsProperty(
                data_storage=elasticache.CfnServerlessCache.DataStorageProperty(
                    unit="GB", maximum=20 if self.config.production else 5
                ),
                ecpu_per_second=elasticache.CfnServerlessCache.ECPUPerSecondProperty(
                    maximum=10_000 if self.config.production else 5_000
                ),
            ),
        )
        self.cache.apply_removal_policy(
            RemovalPolicy.RETAIN if self.config.production else RemovalPolicy.DESTROY
        )
        self.redis_url = (
            f"rediss://{self.cache.attr_endpoint_address}:{self.cache.attr_endpoint_port}/0"
        )

    def _secret(
        self,
        construct_id: str,
        key: str,
        *,
        length: int,
        exclude_punctuation: bool = True,
    ) -> secretsmanager.ISecret:
        if self.config.production:
            return secretsmanager.Secret.from_secret_complete_arn(
                self, construct_id, self.config.secrets[key]
            )
        return secretsmanager.Secret(
            self,
            construct_id,
            secret_name=f"ostiari/{self.config.name}/{key.replace('_', '-')}",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=length,
                exclude_punctuation=exclude_punctuation,
                include_space=False,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

    def _secrets(self) -> None:
        self.jwt_secret = self._secret("JwtSecret", "jwt", length=48)
        self.admin_password = self._secret("AdminPassword", "admin_password", length=24)
        self.config_admin_key = self._secret("ConfigAdminKey", "config_admin_key", length=48)
        self.gateway_agent_token = self._secret(
            "GatewayAgentToken", "gateway_agent_token", length=48
        )
        self.encryption_key = (
            self._secret("EncryptionKey", "encryption_key", length=44)
            if self.config.production
            else None
        )
        self.workload_client_secret = (
            self._secret("WorkloadClientSecret", "workload_client_secret", length=48)
            if self.config.production
            else None
        )

    def _image(
        self,
        key: str,
        dockerfile: str,
        *,
        build_args: dict[str, str] | None = None,
    ) -> ecs.ContainerImage:
        if self.config.production:
            image = self.config.images[key]
            repository_name, digest = image.split("/", 1)[1].rsplit("@", 1)
            repository = ecr.Repository.from_repository_name(
                self,
                f"{key}Repository",
                repository_name,
            )
            return ecs.ContainerImage.from_ecr_repository(repository, digest)
        return ecs.ContainerImage.from_asset(
            str(ROOT),
            file=dockerfile,
            build_args=build_args,
            platform=ecr_assets.Platform.LINUX_AMD64,
        )

    def _task_definition(
        self, construct_id: str, *, cpu: int, memory: int
    ) -> ecs.FargateTaskDefinition:
        return ecs.FargateTaskDefinition(
            self,
            construct_id,
            cpu=cpu,
            memory_limit_mib=memory,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.X86_64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )

    def _service(
        self,
        construct_id: str,
        *,
        task: ecs.FargateTaskDefinition,
        security_group: ec2.ISecurityGroup,
        discovery_name: str,
        container: ecs.ContainerDefinition,
        port: int,
        desired_count: int | None = None,
    ) -> ecs.FargateService:
        service = ecs.FargateService(
            self,
            construct_id,
            cluster=self.cluster,
            task_definition=task,
            desired_count=desired_count or self.config.desired_count,
            assign_public_ip=self.assign_public_ip,
            vpc_subnets=self.app_subnets,
            security_groups=[security_group],
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            health_check_grace_period=Duration.minutes(5),
            min_healthy_percent=50 if not self.config.production else 100,
            max_healthy_percent=200,
            enable_ecs_managed_tags=True,
            cloud_map_options=ecs.CloudMapOptions(
                cloud_map_namespace=self.namespace,
                name=discovery_name,
                container=container,
                container_port=port,
                dns_ttl=Duration.seconds(10),
            ),
        )
        return service

    def _services(self) -> None:
        self.cluster = ecs.Cluster(
            self,
            "Cluster",
            vpc=self.vpc,
            container_insights_v2=(
                ecs.ContainerInsights.ENHANCED
                if self.config.production
                else ecs.ContainerInsights.ENABLED
            ),
        )
        retention = (
            logs.RetentionDays.THREE_MONTHS
            if self.config.production
            else logs.RetentionDays.ONE_WEEK
        )
        control_dns = f"control-plane.{self.config.namespace}"
        gateway_dns = f"gateway.{self.config.namespace}"
        demo_dns = f"demo-tools.{self.config.namespace}"

        backend_task = self._task_definition(
            "ControlPlaneTask",
            cpu=1024 if self.config.production else 512,
            memory=2048 if self.config.production else 1024,
        )
        backend_env = {
            "OSTIARI_DB_HOST": self.database.db_instance_endpoint_address,
            "OSTIARI_DB_PORT": self.database.db_instance_endpoint_port,
            "OSTIARI_DB_NAME": "ostiari",
            "OSTIARI_ENV": "production" if self.config.production else "",
            "OSTIARI_NO_DEMO": "0" if self.config.demo else "1",
            "OSTIARI_TENANCY_MODE": "single",
            "OSTIARI_ORG_ID": self.config.org_id,
            "OSTIARI_CONTROL_PLANE_REPLICAS": str(self.config.desired_count),
            "REDIS_URL": self.redis_url,
            "OSTIARI_GATEWAY_CALLBACK_ALLOW": gateway_dns,
            "OSTIARI_GATEWAY_AGENT_ID": "ostiari-control-plane",
            "OSTIARI_DEMO_GATEWAY_URL": f"http://{gateway_dns}:8421",
            "OSTIARI_DEMO_TOOLS_URL": f"http://{demo_dns}:9300",
        }
        if self.config.production:
            dashboard_url = f"https://{self.config.domains['dashboard']}"
            backend_env.update(
                {
                    "OSTIARI_REQUIRE_AUTH": "true",
                    "OSTIARI_CORS_ORIGINS": dashboard_url,
                    "OSTIARI_FRONTEND_URL": dashboard_url,
                    "OSTIARI_WORKLOAD_OIDC_ISSUER": self.config.auth["workload_issuer"],
                    "OSTIARI_WORKLOAD_OIDC_AUDIENCE": self.config.auth["workload_audience"],
                }
            )
            for source, target in (
                ("browser_oidc_issuer", "OIDC_ISSUER"),
                ("browser_oidc_client_id", "OIDC_CLIENT_ID"),
                ("browser_oidc_redirect_uri", "OIDC_REDIRECT_URI"),
            ):
                if self.config.auth.get(source):
                    backend_env[target] = self.config.auth[source]
        backend_secrets = {
            "OSTIARI_DB_USER": ecs.Secret.from_secrets_manager(self.database.secret, "username"),
            "OSTIARI_DB_PASSWORD": ecs.Secret.from_secrets_manager(
                self.database.secret, "password"
            ),
            "OSTIARI_JWT_SECRET": ecs.Secret.from_secrets_manager(self.jwt_secret),
            "OSTIARI_ADMIN_PASSWORD": ecs.Secret.from_secrets_manager(self.admin_password),
            "OSTIARI_CONFIG_ADMIN_KEY": ecs.Secret.from_secrets_manager(self.config_admin_key),
            "OSTIARI_GATEWAY_AGENT_TOKEN": ecs.Secret.from_secrets_manager(
                self.gateway_agent_token
            ),
        }
        if self.encryption_key:
            backend_secrets["OSTIARI_ENCRYPTION_KEY"] = ecs.Secret.from_secrets_manager(
                self.encryption_key
            )
        if self.config.production and self.config.secrets.get("browser_oidc_client_secret"):
            browser_secret = secretsmanager.Secret.from_secret_complete_arn(
                self,
                "BrowserOidcClientSecret",
                self.config.secrets["browser_oidc_client_secret"],
            )
            backend_secrets["OIDC_CLIENT_SECRET"] = ecs.Secret.from_secrets_manager(browser_secret)
        backend = backend_task.add_container(
            "control-plane",
            container_name="control-plane",
            image=self._image("control_plane", "deploy/docker/Dockerfile.control-plane"),
            command=["python", "-m", "control_plane.aws_entrypoint"],
            environment=backend_env,
            secrets=backend_secrets,
            port_mappings=[ecs.PortMapping(container_port=8400, name="http")],
            logging=ecs.LogDrivers.aws_logs(stream_prefix="control-plane", log_retention=retention),
            health_check=ecs.HealthCheck(
                command=[
                    "CMD-SHELL",
                    'python -c "import urllib.request; '
                    "urllib.request.urlopen('http://localhost:8400/api/health', "
                    'timeout=4).read()" || exit 1',
                ],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                retries=3,
                start_period=Duration.minutes(2),
            ),
            readonly_root_filesystem=True,
            user="10001",
        )
        self.backend_service = self._service(
            "ControlPlaneService",
            task=backend_task,
            security_group=self.backend_sg,
            discovery_name="control-plane",
            container=backend,
            port=8400,
        )
        self.backend_service.node.add_dependency(self.database, self.cache)

        gateway_task = self._task_definition(
            "GatewayTask",
            cpu=1024 if self.config.production else 512,
            memory=2048 if self.config.production else 1024,
        )
        gateway_env = {
            "OSTIARI_GATEWAY_ID": "crm-agent" if self.config.demo else "ostiari-gateway",
            "OSTIARI_CONTROL_PLANE_URL": f"http://{control_dns}:8400",
            "OSTIARI_ADVERTISE_HOST": gateway_dns,
            "OSTIARI_PORT": "8421",
            "OSTIARI_ENV": "production" if self.config.production else "",
            "OSTIARI_TENANCY_MODE": "single",
            "OSTIARI_ORG_ID": self.config.org_id,
            "OSTIARI_REDIS_URL": self.redis_url,
            "OSTIARI_X402_MODE": "off" if self.config.production else "simulated",
        }
        gateway_secrets = {
            "OSTIARI_CONFIG_ADMIN_KEY": ecs.Secret.from_secrets_manager(self.config_admin_key)
        }
        if self.config.production:
            gateway_env.update(
                {
                    "OSTIARI_HITL": "on",
                    "OSTIARI_GATEWAY_AUTH": "required",
                    "OSTIARI_OIDC_ISSUER": self.config.auth["agent_issuer"],
                    "OSTIARI_OIDC_AUDIENCE": self.config.auth["agent_audience"],
                    "OSTIARI_REQUIRE_REDIS": "true",
                    "OSTIARI_GATEWAY_RATE_LIMIT_RPM": self.config.auth.get(
                        "gateway_rate_limit_rpm", "600"
                    ),
                    "OSTIARI_FAIL_CLOSED_ON_CP_LOSS": "true",
                    "OSTIARI_REQUIRE_AXON": "true",
                    "OSTIARI_WORKLOAD_TOKEN_URL": self.config.auth["workload_token_url"],
                    "OSTIARI_WORKLOAD_CLIENT_ID": self.config.auth["gateway_client_id"],
                    "OSTIARI_WORKLOAD_TOKEN_AUDIENCE": self.config.auth["workload_audience"],
                }
            )
            if self.workload_client_secret is None:
                raise ValueError("production workload client secret is missing")
            gateway_secrets["OSTIARI_WORKLOAD_CLIENT_SECRET"] = ecs.Secret.from_secrets_manager(
                self.workload_client_secret
            )
        gateway = gateway_task.add_container(
            "gateway",
            container_name="gateway",
            image=self._image("gateway", "deploy/docker/Dockerfile.gateway"),
            environment=gateway_env,
            secrets=gateway_secrets,
            port_mappings=[ecs.PortMapping(container_port=8421, name="http")],
            logging=ecs.LogDrivers.aws_logs(stream_prefix="gateway", log_retention=retention),
            health_check=ecs.HealthCheck(
                command=[
                    "CMD-SHELL",
                    'python -c "import urllib.request; '
                    "urllib.request.urlopen('http://localhost:8421/health', "
                    'timeout=4).read()" || exit 1',
                ],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                retries=3,
                start_period=Duration.minutes(2),
            ),
            readonly_root_filesystem=True,
            user="10001",
        )
        self.gateway_service = self._service(
            "GatewayService",
            task=gateway_task,
            security_group=self.gateway_sg,
            discovery_name="gateway",
            container=gateway,
            port=8421,
        )
        self.gateway_service.node.add_dependency(self.backend_service, self.cache)

        frontend_task = self._task_definition("FrontendTask", cpu=256, memory=512)
        frontend = frontend_task.add_container(
            "frontend",
            container_name="frontend",
            image=self._image(
                "frontend",
                "deploy/docker/Dockerfile.frontend",
                build_args={"VITE_API_URL": ""},
            ),
            port_mappings=[ecs.PortMapping(container_port=9000, name="http")],
            logging=ecs.LogDrivers.aws_logs(stream_prefix="frontend", log_retention=retention),
            health_check=ecs.HealthCheck(
                command=[
                    "CMD-SHELL",
                    "wget -q -O /dev/null http://localhost:9000/ || exit 1",
                ],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                retries=3,
            ),
            readonly_root_filesystem=True,
            user="101",
        )
        self.frontend_service = self._service(
            "FrontendService",
            task=frontend_task,
            security_group=self.frontend_sg,
            discovery_name="frontend",
            container=frontend,
            port=9000,
        )

        self.demo_service = None
        if self.config.demo:
            if self.demo_sg is None:
                raise ValueError("demo security group is missing")
            demo_task = self._task_definition("DemoToolsTask", cpu=256, memory=512)
            demo = demo_task.add_container(
                "demo-tools",
                container_name="demo-tools",
                image=self._image("demo", "deploy/docker/Dockerfile.demo"),
                port_mappings=[ecs.PortMapping(container_port=9300, name="http")],
                logging=ecs.LogDrivers.aws_logs(
                    stream_prefix="demo-tools", log_retention=retention
                ),
                health_check=ecs.HealthCheck(
                    command=[
                        "CMD-SHELL",
                        'python -c "import urllib.request; '
                        "urllib.request.urlopen('http://localhost:9300/health', "
                        'timeout=4).read()" || exit 1',
                    ],
                    interval=Duration.seconds(30),
                    timeout=Duration.seconds(5),
                    retries=3,
                ),
                readonly_root_filesystem=True,
                user="10001",
            )
            self.demo_service = self._service(
                "DemoToolsService",
                task=demo_task,
                security_group=self.demo_sg,
                discovery_name="demo-tools",
                container=demo,
                port=9300,
                desired_count=1,
            )
            self.gateway_service.node.add_dependency(self.demo_service)

        if self.config.production:
            for service, maximum in (
                (self.backend_service, 6),
                (self.gateway_service, 10),
                (self.frontend_service, 4),
            ):
                scaling = service.auto_scale_task_count(
                    min_capacity=self.config.desired_count,
                    max_capacity=maximum,
                )
                scaling.scale_on_cpu_utilization("CpuScaling", target_utilization_percent=60)

    def _load_balancer(self) -> None:
        self.alb = elbv2.ApplicationLoadBalancer(
            self,
            "LoadBalancer",
            vpc=self.vpc,
            internet_facing=True,
            security_group=self.alb_sg,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            drop_invalid_header_fields=True,
            deletion_protection=self.config.production,
        )
        if self.config.production:
            self.access_logs = s3.Bucket(
                self,
                "LoadBalancerLogs",
                encryption=s3.BucketEncryption.S3_MANAGED,
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                enforce_ssl=True,
                lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(90))],
                removal_policy=RemovalPolicy.RETAIN,
            )
            self.alb.log_access_logs(self.access_logs, prefix="alb")

            certificate = acm.Certificate.from_certificate_arn(
                self, "Certificate", self.config.domains["certificate_arn"]
            )
            listener = self.alb.add_listener(
                "Https",
                port=443,
                protocol=elbv2.ApplicationProtocol.HTTPS,
                certificates=[certificate],
                ssl_policy=elbv2.SslPolicy.RECOMMENDED_TLS,
                open=False,
            )
            redirect = self.alb.add_listener(
                "HttpRedirect",
                port=80,
                protocol=elbv2.ApplicationProtocol.HTTP,
                open=False,
                default_action=elbv2.ListenerAction.redirect(
                    protocol="HTTPS", port="443", permanent=True
                ),
            )
            del redirect
            listener.add_targets(
                "FrontendTargets",
                port=9000,
                protocol=elbv2.ApplicationProtocol.HTTP,
                targets=[
                    self.frontend_service.load_balancer_target(
                        container_name="frontend", container_port=9000
                    )
                ],
                health_check=elbv2.HealthCheck(path="/", healthy_http_codes="200"),
            )
            listener.add_targets(
                "ControlPlaneTargets",
                port=8400,
                protocol=elbv2.ApplicationProtocol.HTTP,
                targets=[
                    self.backend_service.load_balancer_target(
                        container_name="control-plane", container_port=8400
                    )
                ],
                priority=10,
                conditions=[
                    elbv2.ListenerCondition.host_headers([self.config.domains["dashboard"]]),
                    elbv2.ListenerCondition.path_patterns(
                        ["/api/*", "/docs*", "/openapi.json", "/ws/*"]
                    ),
                ],
                health_check=elbv2.HealthCheck(path="/api/ready", healthy_http_codes="200"),
            )
            listener.add_targets(
                "GatewayTargets",
                port=8421,
                protocol=elbv2.ApplicationProtocol.HTTP,
                targets=[
                    self.gateway_service.load_balancer_target(
                        container_name="gateway", container_port=8421
                    )
                ],
                priority=20,
                conditions=[elbv2.ListenerCondition.host_headers([self.config.domains["gateway"]])],
                health_check=elbv2.HealthCheck(path="/ready", healthy_http_codes="200"),
            )
            self.dashboard_url = f"https://{self.config.domains['dashboard']}"
            self.gateway_url = f"https://{self.config.domains['gateway']}"
            self._dns()
        else:
            listener = self.alb.add_listener(
                "Http",
                port=80,
                protocol=elbv2.ApplicationProtocol.HTTP,
                open=False,
            )
            listener.add_targets(
                "FrontendTargets",
                port=9000,
                protocol=elbv2.ApplicationProtocol.HTTP,
                targets=[
                    self.frontend_service.load_balancer_target(
                        container_name="frontend", container_port=9000
                    )
                ],
                health_check=elbv2.HealthCheck(path="/", healthy_http_codes="200"),
            )
            listener.add_targets(
                "ControlPlaneTargets",
                port=8400,
                protocol=elbv2.ApplicationProtocol.HTTP,
                targets=[
                    self.backend_service.load_balancer_target(
                        container_name="control-plane", container_port=8400
                    )
                ],
                priority=10,
                conditions=[
                    elbv2.ListenerCondition.path_patterns(
                        ["/api/*", "/docs*", "/openapi.json", "/ws/*"]
                    )
                ],
                health_check=elbv2.HealthCheck(path="/api/ready", healthy_http_codes="200"),
            )
            gateway_listener = self.alb.add_listener(
                "GatewayHttp",
                port=8421,
                protocol=elbv2.ApplicationProtocol.HTTP,
                open=False,
            )
            gateway_listener.add_targets(
                "GatewayTargets",
                port=8421,
                protocol=elbv2.ApplicationProtocol.HTTP,
                targets=[
                    self.gateway_service.load_balancer_target(
                        container_name="gateway", container_port=8421
                    )
                ],
                health_check=elbv2.HealthCheck(path="/ready", healthy_http_codes="200"),
            )
            self.dashboard_url = f"http://{self.alb.load_balancer_dns_name}"
            self.gateway_url = f"http://{self.alb.load_balancer_dns_name}:8421"

    def _dns(self) -> None:
        zone_id = self.config.domains.get("hosted_zone_id")
        zone_name = self.config.domains.get("hosted_zone_name")
        if not zone_id and not zone_name:
            return
        if not zone_id or not zone_name:
            raise ValueError("domains.hosted_zone_id and hosted_zone_name must be set together")
        zone = route53.HostedZone.from_hosted_zone_attributes(
            self,
            "HostedZone",
            hosted_zone_id=zone_id,
            zone_name=zone_name,
        )
        for construct_id, hostname in (
            ("DashboardRecord", self.config.domains["dashboard"]),
            ("GatewayRecord", self.config.domains["gateway"]),
        ):
            route53.ARecord(
                self,
                construct_id,
                zone=zone,
                record_name=hostname,
                target=route53.RecordTarget.from_alias(
                    route53_targets.LoadBalancerTarget(self.alb)
                ),
            )

    def _agentcore(self) -> None:
        self.agentcore_sg = ec2.SecurityGroup(
            self, "AgentCoreSecurityGroup", vpc=self.vpc, allow_all_outbound=True
        )
        self.gateway_sg.add_ingress_rule(self.agentcore_sg, ec2.Port.tcp(8421), "AgentCore bridge")
        runtime_name = re.sub(r"[^A-Za-z0-9_]", "_", f"Ostiari_{self.config.name}")[:48]
        role = iam.Role(
            self,
            "AgentCoreExecutionRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": self.format_arn(
                            service="bedrock-agentcore",
                            resource="runtime",
                            resource_name=f"{runtime_name}*",
                            arn_format=ArnFormat.SLASH_RESOURCE_NAME,
                        )
                    },
                },
            ),
            description="Runs the Ostiari AgentCore governance bridge",
        )
        runtime_log_group_arn = self.format_arn(
            service="logs",
            resource="log-group",
            resource_name=f"/aws/bedrock-agentcore/runtimes/{runtime_name}-*",
            arn_format=ArnFormat.COLON_RESOURCE_NAME,
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:DescribeLogStreams", "logs:CreateLogGroup"],
                resources=[
                    self.format_arn(
                        service="logs",
                        resource="log-group",
                        resource_name="/aws/bedrock-agentcore/runtimes/*",
                        arn_format=ArnFormat.COLON_RESOURCE_NAME,
                    )
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:PutResourcePolicy"],
                resources=[runtime_log_group_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:DescribeLogGroups"],
                resources=[
                    self.format_arn(
                        service="logs",
                        resource="log-group",
                        resource_name="*",
                        arn_format=ArnFormat.COLON_RESOURCE_NAME,
                    )
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[
                    self.format_arn(
                        service="logs",
                        resource="log-group",
                        resource_name="/aws/bedrock-agentcore/runtimes/*:log-stream:*",
                        arn_format=ArnFormat.COLON_RESOURCE_NAME,
                    )
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                ],
                resources=["*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}},
            )
        )
        environment = {
            "OSTIARI_GATEWAY_URL": (f"http://gateway.{self.config.namespace}:8421"),
            "OSTIARI_AGENT_ID": "agentcore-runtime",
            "OSTIARI_FRAMEWORK": "bedrock-agentcore",
        }
        if self.config.production:
            secret = secretsmanager.Secret.from_secret_complete_arn(
                self,
                "AgentCoreClientSecret",
                self.config.secrets["agentcore_client_secret"],
            )
            secret.grant_read(role)
            environment.update(
                {
                    "OSTIARI_AGENT_ID": self.config.auth["agentcore_client_id"],
                    "OSTIARI_AGENT_TOKEN_URL": self.config.auth["agentcore_token_url"],
                    "OSTIARI_AGENT_CLIENT_ID": self.config.auth["agentcore_client_id"],
                    "OSTIARI_AGENT_CLIENT_SECRET_ARN": secret.secret_arn,
                    "OSTIARI_AGENT_AUDIENCE": self.config.auth.get(
                        "agentcore_audience", self.config.auth["agent_audience"]
                    ),
                }
            )
        artifact = (
            agentcore.AgentRuntimeArtifact.from_image_uri(self.config.images["agentcore"])
            if self.config.production
            else agentcore.AgentRuntimeArtifact.from_asset(
                str(ROOT),
                file="deploy/agentcore/Dockerfile",
                platform=ecr_assets.Platform.LINUX_ARM64,
            )
        )
        if self.config.production:
            repository_name = self.config.images["agentcore"].split("/", 1)[1].split("@", 1)[0]
            role.add_to_policy(
                iam.PolicyStatement(
                    actions=[
                        "ecr:BatchCheckLayerAvailability",
                        "ecr:GetDownloadUrlForLayer",
                        "ecr:BatchGetImage",
                    ],
                    resources=[
                        self.format_arn(
                            service="ecr",
                            resource="repository",
                            resource_name=repository_name,
                            arn_format=ArnFormat.SLASH_RESOURCE_NAME,
                        )
                    ],
                )
            )
            role.add_to_policy(
                iam.PolicyStatement(
                    actions=["ecr:GetAuthorizationToken"],
                    resources=["*"],
                )
            )
        self.agentcore_runtime = agentcore.Runtime(
            self,
            "AgentCoreRuntime",
            runtime_name=runtime_name,
            description="Ostiari-governed AgentCore validation bridge",
            agent_runtime_artifact=artifact,
            execution_role=role,
            environment_variables=environment,
            authorizer_configuration=agentcore.RuntimeAuthorizerConfiguration.using_iam(),
            network_configuration=agentcore.RuntimeNetworkConfiguration.using_vpc(
                self,
                vpc=self.vpc,
                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
                security_groups=[self.agentcore_sg],
            ),
            protocol_configuration=agentcore.ProtocolType.HTTP,
            tracing_enabled=True,
        )
        self.agentcore_runtime.node.add_dependency(self.gateway_service)

    def _production_controls(self) -> None:
        visibility = wafv2.CfnWebACL.VisibilityConfigProperty(
            cloud_watch_metrics_enabled=True,
            metric_name=f"Ostiari{self.config.name}WebAcl",
            sampled_requests_enabled=True,
        )
        web_acl = wafv2.CfnWebACL(
            self,
            "WebAcl",
            scope="REGIONAL",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=visibility,
            rules=[
                wafv2.CfnWebACL.RuleProperty(
                    name="IpRateLimit",
                    priority=0,
                    action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                            aggregate_key_type="IP",
                            limit=2_000,
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="OstiariIpRateLimit",
                        sampled_requests_enabled=True,
                    ),
                )
            ],
        )
        wafv2.CfnWebACLAssociation(
            self,
            "WebAclAssociation",
            resource_arn=self.alb.load_balancer_arn,
            web_acl_arn=web_acl.attr_arn,
        )

        topic = (
            sns.Topic.from_topic_arn(self, "AlarmTopic", self.config.alarm_topic_arn)
            if self.config.alarm_topic_arn
            else None
        )
        alarms = [
            cloudwatch.Alarm(
                self,
                "LoadBalancer5xxAlarm",
                metric=cloudwatch.Metric(
                    namespace="AWS/ApplicationELB",
                    metric_name="HTTPCode_ELB_5XX_Count",
                    dimensions_map={"LoadBalancer": self.alb.load_balancer_full_name},
                    statistic="Sum",
                    period=Duration.minutes(5),
                ),
                threshold=5,
                evaluation_periods=2,
                datapoints_to_alarm=2,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            ),
            cloudwatch.Alarm(
                self,
                "DatabaseCpuAlarm",
                metric=self.database.metric_cpu_utilization(period=Duration.minutes(5)),
                threshold=80,
                evaluation_periods=3,
                datapoints_to_alarm=2,
            ),
        ]
        if topic:
            for alarm in alarms:
                alarm.add_alarm_action(cloudwatch_actions.SnsAction(topic))

    def _outputs(self) -> None:
        CfnOutput(self, "DashboardUrl", value=self.dashboard_url)
        CfnOutput(self, "GatewayUrl", value=self.gateway_url)
        CfnOutput(
            self,
            "AdminSecretArn",
            value=self.admin_password.secret_arn,
            description="Retrieve directly from Secrets Manager; never place it in logs",
        )
        CfnOutput(self, "AdminEmail", value="admin@ostiari.ai")
        CfnOutput(
            self,
            "DatabaseSecretArn",
            value=self.database.secret.secret_arn,
        )
        CfnOutput(
            self,
            "DeploymentProfile",
            value=self.config.profile,
        )
        if self.config.agentcore:
            CfnOutput(
                self,
                "AgentCoreRuntimeArn",
                value=self.agentcore_runtime.agent_runtime_arn,
            )
