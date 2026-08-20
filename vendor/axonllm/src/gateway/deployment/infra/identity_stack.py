"""Retained Cognito identity for first-time AxonLLM AgentCore adopters."""

from aws_cdk import (
    CfnCondition,
    CfnOutput,
    CfnParameter,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
    aws_cognito as cognito,
)
from constructs import Construct


TENANT_CLAIM_NAME = "custom:tenant_id"
PROJECT_CLAIM_NAME = "custom:project_id"


class AxonLLMIdentityStack(Stack):
    """Operator-managed workforce identity with no self-service enrollment."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        deployment_namespace: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        physical_suffix = (
            f"-{deployment_namespace}" if deployment_namespace else ""
        )
        removal_policy = (
            RemovalPolicy.DESTROY
            if deployment_namespace
            else RemovalPolicy.RETAIN
        )
        deletion_protection = not bool(deployment_namespace)

        endpoint_mode = CfnParameter(
            self,
            "EndpointMode",
            type="String",
            default="custom-domain",
            allowed_values=["custom-domain", "cloudfront"],
            description=(
                "Control-plane endpoint architecture. Existing deployments "
                "default to custom-domain."
            ),
        )
        custom_domain_mode = CfnCondition(
            self,
            "CustomDomainEndpoint",
            expression=Fn.condition_equals(
                endpoint_mode.value_as_string,
                "custom-domain",
            ),
        )
        hosted_ui_domain_prefix = CfnParameter(
            self,
            "HostedUiDomainPrefix",
            type="String",
            min_length=3,
            max_length=63,
            allowed_pattern=r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$",
            constraint_description=(
                "must be 3-63 lowercase letters, numbers, or hyphens, "
                "starting and ending with a letter or number"
            ),
            description=(
                "Globally unique Cognito managed-login domain prefix"
            ),
        )
        control_plane_domain_name = CfnParameter(
            self,
            "ControlPlaneDomainInput",
            type="String",
            default="",
            max_length=253,
            allowed_pattern=(
                r"^(?:|(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}"
                r"[a-z0-9])?\.)+[a-z]{2,63})$"
            ),
            constraint_description=(
                "must be empty or a lowercase fully qualified DNS hostname"
            ),
            description=(
                "Stable control-plane hostname used for the ALB OAuth callback"
            ),
        )
        control_plane_domain_name.override_logical_id(
            "ControlPlaneDomainName"
        )
        control_plane_callback_url = Fn.join(
            "",
            [
                "https://",
                control_plane_domain_name.value_as_string,
                "/oauth2/idpresponse",
            ],
        )
        ses_from_email = CfnParameter(
            self,
            "SesFromEmail",
            type="String",
            min_length=6,
            max_length=320,
            allowed_pattern=(
                r"^[^@\s]+@(?:[a-z0-9](?:[a-z0-9-]{0,61}"
                r"[a-z0-9])?\.)+[a-z]{2,63}$"
            ),
            constraint_description=(
                "must be a verified SES email address with a lowercase "
                "fully qualified domain"
            ),
            description=(
                "Verified SES sender used for Cognito invitations and recovery"
            ),
        )
        # The logical ID is retained for setup-file compatibility. The value
        # can be either a verified domain or the exact verified sender address.
        ses_source_identity = CfnParameter(
            self,
            "SesVerifiedDomain",
            type="String",
            min_length=4,
            max_length=320,
            allowed_pattern=(
                r"^(?:[^@\s]+@)?(?:[a-z0-9](?:[a-z0-9-]{0,61}"
                r"[a-z0-9])?\.)+[a-z]{2,63}$"
            ),
            constraint_description=(
                "must be either SesFromEmail exactly or its lowercase "
                "SES-verified domain"
            ),
            description=(
                "SES-verified source identity used by the Cognito user pool"
            ),
        )

        user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"axonllm-agentcore-users{physical_suffix}",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            sign_in_case_sensitive=False,
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(
                    required=True,
                    mutable=True,
                ),
            ),
            custom_attributes={
                "tenant_id": cognito.StringAttribute(
                    min_len=1,
                    max_len=128,
                    mutable=True,
                ),
                "project_id": cognito.StringAttribute(
                    min_len=1,
                    max_len=128,
                    mutable=True,
                ),
            },
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            email=cognito.UserPoolEmail.with_ses(
                from_email=ses_from_email.value_as_string,
                from_name="AxonLLM",
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=14,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
                temp_password_validity=Duration.days(7),
            ),
            mfa=cognito.Mfa.REQUIRED,
            mfa_second_factor=cognito.MfaSecondFactor(
                sms=False,
                otp=True,
            ),
            user_invitation=cognito.UserInvitationConfig(
                email_subject="Your AxonLLM invitation",
                email_body=(
                    "Your AxonLLM username is {username} and your temporary "
                    "password is {####}. Sign in and enroll TOTP MFA before "
                    "using the AgentCore runtime."
                ),
            ),
            deletion_protection=deletion_protection,
            removal_policy=removal_policy,
        )
        user_pool.node.default_child.add_property_override(
            "EmailConfiguration.SourceArn",
            self.format_arn(
                service="ses",
                resource="identity",
                resource_name=ses_source_identity.value_as_string,
            ),
        )

        readable_attributes = (
            cognito.ClientAttributes()
            .with_standard_attributes(
                email=True,
                email_verified=True,
            )
            .with_custom_attributes("tenant_id", "project_id")
        )
        writable_attributes = (
            cognito.ClientAttributes().with_standard_attributes(email=True)
        )
        app_client = user_pool.add_client(
            # Preserve the retained resource identity and physical name so an
            # existing PKCE client is disabled in place instead of orphaned.
            "PublicPkceClient",
            user_pool_client_name=(
                f"axonllm-agentcore-pkce{physical_suffix}"
            ),
            generate_secret=False,
            prevent_user_existence_errors=True,
            enable_token_revocation=True,
            disable_o_auth=True,
            # Browser OAuth is owned by the control-plane client. This
            # secretless client is a stable AgentCore JWT audience only.
            auth_flows=cognito.AuthFlow(),
            access_token_validity=Duration.minutes(15),
            id_token_validity=Duration.minutes(15),
            refresh_token_validity=Duration.hours(8),
            refresh_token_rotation_grace_period=Duration.seconds(0),
            read_attributes=readable_attributes,
            write_attributes=writable_attributes,
        )
        app_client.apply_removal_policy(removal_policy)
        certification_client = user_pool.add_client(
            "ConfidentialCertificationClient",
            user_pool_client_name=(
                f"axonllm-agentcore-certification{physical_suffix}"
            ),
            generate_secret=True,
            prevent_user_existence_errors=True,
            enable_token_revocation=True,
            disable_o_auth=True,
            # This confidential client is restricted to deployment automation.
            # Workforce clients retain authorization-code plus PKCE only.
            auth_flows=cognito.AuthFlow(
                admin_user_password=True,
            ),
            access_token_validity=(
                Duration.hours(6)
                if deployment_namespace
                else Duration.minutes(15)
            ),
            id_token_validity=(
                Duration.hours(6)
                if deployment_namespace
                else Duration.minutes(15)
            ),
            refresh_token_validity=(
                Duration.hours(6)
                if deployment_namespace
                else Duration.hours(1)
            ),
            read_attributes=readable_attributes,
        )
        certification_client.apply_removal_policy(removal_policy)
        alb_client = user_pool.add_client(
            "ConfidentialAlbClient",
            user_pool_client_name=(
                f"axonllm-control-plane-alb{physical_suffix}"
            ),
            generate_secret=True,
            prevent_user_existence_errors=True,
            enable_token_revocation=True,
            # ALB performs the authorization-code exchange server-side. Direct
            # password, SRP, implicit, and client-credentials flows stay off.
            auth_flows=cognito.AuthFlow(),
            access_token_validity=Duration.minutes(15),
            id_token_validity=Duration.minutes(15),
            refresh_token_validity=Duration.hours(8),
            refresh_token_rotation_grace_period=Duration.seconds(0),
            read_attributes=readable_attributes,
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.COGNITO
            ],
            o_auth=cognito.OAuthSettings(
                callback_urls=[control_plane_callback_url],
                flows=cognito.OAuthFlows(
                    authorization_code_grant=True,
                    implicit_code_grant=False,
                    client_credentials=False,
                ),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
            ),
        )
        alb_client.apply_removal_policy(removal_policy)
        cfn_alb_client = alb_client.node.default_child
        if not isinstance(cfn_alb_client, cognito.CfnUserPoolClient):
            raise RuntimeError("ALB Cognito client did not synthesize")
        cfn_alb_client.cfn_options.condition = custom_domain_mode

        hosted_ui_domain = user_pool.add_domain(
            "HostedUiDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=hosted_ui_domain_prefix.value_as_string,
            ),
            managed_login_version=(
                cognito.ManagedLoginVersion.CLASSIC_HOSTED_UI
            ),
        )
        hosted_ui_domain.apply_removal_policy(removal_policy)

        issuer = Fn.join(
            "",
            [
                "https://cognito-idp.",
                self.region,
                ".",
                self.url_suffix,
                "/",
                user_pool.user_pool_id,
            ],
        )
        discovery_url = Fn.join(
            "",
            [issuer, "/.well-known/openid-configuration"],
        )

        CfnOutput(
            self,
            "UserPoolId",
            value=user_pool.user_pool_id,
            description="Cognito user pool used by AxonLLM",
        )
        CfnOutput(
            self,
            "UserPoolArn",
            value=user_pool.user_pool_arn,
            description="Cognito user pool imported by the control plane",
            export_name=Fn.join(
                ":",
                [self.stack_name, "UserPoolArn"],
            ),
        )
        CfnOutput(
            self,
            "OidcIssuer",
            value=issuer,
            description="Exact OIDC issuer accepted by AxonLLM",
            export_name=Fn.join(
                ":",
                [self.stack_name, "OidcIssuer"],
            ),
        )
        CfnOutput(
            self,
            "OidcDiscoveryUrl",
            value=discovery_url,
            description="OIDC discovery URL for the AgentCore authorizer",
        )
        CfnOutput(
            self,
            "OidcClientId",
            value=app_client.user_pool_client_id,
            description="Secretless AgentCore JWT audience client ID",
        )
        CfnOutput(
            self,
            "OidcAudience",
            value=app_client.user_pool_client_id,
            description="Expected Cognito ID-token audience",
        )
        CfnOutput(
            self,
            "AlbClientId",
            value=Fn.condition_if(
                custom_domain_mode.logical_id,
                alb_client.user_pool_client_id,
                "",
            ).to_string(),
            description=(
                "Confidential authorization-code client used by the "
                "control-plane ALB"
            ),
            export_name=Fn.join(
                ":",
                [self.stack_name, "AlbClientId"],
            ),
        )
        CfnOutput(
            self,
            "CertificationClientId",
            value=certification_client.user_pool_client_id,
            description=(
                "Confidential client used only for fresh launch-certification "
                "tokens"
            ),
        )
        CfnOutput(
            self,
            "ControlPlaneDomainName",
            value=Fn.condition_if(
                custom_domain_mode.logical_id,
                control_plane_domain_name.value_as_string,
                "",
            ).to_string(),
            description=(
                "Stable hostname configured on the confidential ALB client"
            ),
            export_name=Fn.join(
                ":",
                [self.stack_name, "ControlPlaneDomainName"],
            ),
        )
        endpoint_mode_output = CfnOutput(
            self,
            "EndpointModeOutput",
            value=endpoint_mode.value_as_string,
            description="Selected control-plane endpoint architecture",
        )
        endpoint_mode_output.override_logical_id("EndpointMode")
        CfnOutput(
            self,
            "HostedUiDomain",
            value=hosted_ui_domain.base_url(),
            description="Cognito hosted UI base URL",
        )
        CfnOutput(
            self,
            "HostedUiDomainName",
            value=hosted_ui_domain.domain_name,
            description="Cognito domain imported by the control-plane ALB",
            export_name=Fn.join(
                ":",
                [self.stack_name, "HostedUiDomainName"],
            ),
        )
        CfnOutput(
            self,
            "TenantClaimName",
            value=TENANT_CLAIM_NAME,
            export_name=Fn.join(
                ":",
                [self.stack_name, "TenantClaimName"],
            ),
        )
        CfnOutput(
            self,
            "ProjectClaimName",
            value=PROJECT_CLAIM_NAME,
            export_name=Fn.join(
                ":",
                [self.stack_name, "ProjectClaimName"],
            ),
        )
