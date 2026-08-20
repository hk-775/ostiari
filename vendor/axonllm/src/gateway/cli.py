"""AxonLLM CLI — start the gateway, run demos, and manage configuration."""

import argparse
import json
import os
import sys


def _failure_hint(exc: Exception, port: int) -> str:
    """Explain a failed CLI request in terms of what actually went wrong.

    "Is the server running?" was printed for every exception, including 401 and
    403 — which are proof that it *is* running and only the credential is
    missing. Sending someone to restart a healthy gateway is worse than saying
    nothing, so the auth codes get their own message.
    """
    status = getattr(exc, "code", None)
    if status in (401, 403):
        return (
            f"The gateway on port {port} answered {status}, so it is running and "
            "enforcing auth. Pass -k/--api-key or set AXON_API_KEY. Mint a key "
            "with: uv run axon issue-key --project <id> --name cli"
        )
    return f"Is the server running on port {port}? Try: uv run axon serve"


def cmd_demo(args):
    """Start the server and generate real traffic for a live demo."""
    os.execv(
        sys.executable,
        [sys.executable, "-m", "src.gateway.local_demo"],
    )


def cmd_serve(args):
    """Start the AxonLLM gateway server."""
    os.environ["AXON_LOAD_DEMO_DATA"] = "true" if args.demo_data else ""
    os.execv(
        sys.executable,
        [sys.executable, "-m", "src.gateway.local_server"],
    )


def cmd_issue_key(args):
    """Mint an API key directly (in-process), bypassing the admin HTTP endpoint.

    Solves the bootstrap chicken-and-egg: under ENFORCE, POST /admin/projects/{id}/keys
    itself requires an admin credential, so there's no way to get the *first* key over
    HTTP. This mints one via APIKeyService against the same persistence the server uses.

    --project is an authorization scope, not a foreign key: it bounds what the key
    may reach (see key_routes.issue_key), and the project record itself need not
    exist. Requiring one would reintroduce the very bootstrap problem this command
    solves. So a missing project is legal, and this only says so — see below.
    """
    import asyncio

    from src.gateway.auth.api_key_service import APIKeyService
    from src.gateway.persistence import DynamoPersistence

    persistence = DynamoPersistence(region=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    service = APIKeyService(persistence=persistence)

    async def _issue():
        if persistence.enabled:
            await persistence.create_table_if_not_exists()
        tenant_id = getattr(args, "tenant", None)
        default_scopes = (
            ["model.list", "inference.invoke", "query.select"]
            if tenant_id
            else ["chat"]
        )
        scopes = args.scopes.split(",") if args.scopes else default_scopes
        scopes = [scope.strip() for scope in scopes if scope.strip()]
        if tenant_id:
            legacy_admin = sorted(
                scope for scope in scopes if scope.startswith("admin:")
            )
            if legacy_admin:
                raise ValueError(
                    "canonical service keys cannot carry legacy admin scopes: "
                    + ", ".join(legacy_admin)
                )
        _, raw_key = await service.issue_key(
            project_id=args.project,
            name=args.name,
            scopes=scopes,
            created_by="cli",
            tenant_id=tenant_id,
        )
        # Checked after minting, never before: this is a note, not a precondition,
        # and a failed read must not cost anyone their key. get_project() returns
        # None for a transient DynamoDB error as well as for a genuine absence,
        # which is why the note below is worded as a suggestion.
        project_exists = (
            await persistence.get_project(args.project, tenant_id)
            if tenant_id
            else await persistence.get_project(args.project)
        ) is not None
        return raw_key, project_exists

    raw_key, project_exists = asyncio.run(_issue())
    if persistence.enabled and not project_exists:
        print(
            f"\033[33mNote:\033[0m no project '{args.project}' was found. The key still "
            "works — a project id scopes a key rather than pointing at a record — but "
            "until the project exists it will not appear in /admin/projects and has no "
            "budget_limit, so its spend accrues unbudgeted. Create it with:\n"
            f"  curl -X POST localhost:8000/admin/projects -H 'Content-Type: application/json' \\\n"
            f"    -d '{{\"project_id\": \"{args.project}\", \"name\": \"{args.project}\", "
            '"budget_limit": 100.0}\'',
            file=sys.stderr,
        )
    if not persistence.enabled:
        print(
            "\033[33mWarning:\033[0m LLM_ROUTER_DYNAMODB_ENABLED is not 'true', so this key was "
            "NOT persisted and will not be recognized by a running server. Enable persistence "
            "(and point AXON_DYNAMODB_TABLE at the server's table) to mint a usable key.",
            file=sys.stderr,
        )
    tenant_label = (
        f" in tenant '{args.tenant}'"
        if getattr(args, "tenant", None)
        else ""
    )
    print(
        f"\033[1mAPI key issued for project '{args.project}'"
        f"{tenant_label}:\033[0m"
    )
    print(raw_key)
    print("\n\033[2mStore this now — it is shown only once. Use it as:")
    print("  Authorization: Bearer <key>   or   X-Api-Key: <key>\033[0m")


def cmd_bootstrap_tenant(args):
    """Provision the first canonical tenant administrator and project."""
    import asyncio

    from src.gateway.auth.tenant_bootstrap import bootstrap_tenant
    from src.gateway.persistence import DynamoPersistence

    persistence = DynamoPersistence(
        region=os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    )
    result = asyncio.run(
        bootstrap_tenant(
            persistence,
            tenant_id=args.tenant,
            project_id=args.project,
            project_name=args.project_name or args.project,
            issuer=args.issuer,
            subject=args.subject,
            user_name=args.user_name,
            display_name=args.display_name,
            email=args.email,
            budget_limit=args.budget_limit,
        )
    )
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))


def cmd_chat(args):
    """Send a quick chat message via the gateway."""
    import json
    import urllib.request

    base = f"http://localhost:{args.port}"
    payload = json.dumps({
        "model": args.model,
        "messages": [{"role": "user", "content": " ".join(args.message)}],
    }).encode()

    headers = {"Content-Type": "application/json"}
    api_key = args.api_key or os.environ.get("AXON_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        f"{base}/api/chat",
        data=payload,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            print(f"\033[1m{data.get('model', args.model)}\033[0m ({data.get('provider', '?')})")
            print(data.get("content", data.get("error", {}).get("message", "No response")))
            usage = data.get("usage", {})
            if usage:
                print(f"\n\033[2m{usage.get('total_tokens', 0)} tokens\033[0m")
    except Exception as e:
        print(f"Error: {e}")
        print(_failure_hint(e, args.port))
        sys.exit(1)


def cmd_models(args):
    """List available models."""
    import json
    import urllib.request

    base = f"http://localhost:{args.port}"
    api_key = args.api_key or os.environ.get("AXON_API_KEY")
    models_req = urllib.request.Request(f"{base}/api/models")
    if api_key:
        models_req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(models_req, timeout=5) as resp:
            models = json.loads(resp.read())
            print(f"\033[1m{len(models)} models available:\033[0m\n")
            for m in models:
                providers = ", ".join(m.get("providers", []))
                strategy = m.get("routing_strategy", "")
                print(f"  {m['name']:<28} {providers:<20} ({strategy})")
    except Exception as e:
        print(f"Error: {e}\n{_failure_hint(e, args.port)}")
        sys.exit(1)


def cmd_deploy_plan(args):
    """Create immutable deployment artifacts without contacting AWS."""

    from src.gateway.deployment.planning import create_deployment_plan

    plan, plan_path, descriptor_path = create_deployment_plan(
        config_path=args.config,
        context_path=args.context,
        output_directory=args.output_dir,
    )
    print(
        json.dumps(
            {
                "plan_id": plan["plan_id"],
                "plan_path": str(plan_path),
                "descriptor_id": plan["descriptor"]["descriptor_id"],
                "descriptor_path": str(descriptor_path),
                "mutating": False,
                "replacement_review_required": plan["summary"]["replacement_review_required"],
                "chargeable_networking": plan["summary"]["chargeable_networking"],
            },
            indent=2,
            sort_keys=True,
        )
    )


def cmd_deploy_edge_plan(args):
    """Create a qualified edge-transition plan without contacting AWS."""

    from src.gateway.deployment.edge_transition import (
        create_edge_transition_plan,
    )

    plan, plan_path = create_edge_transition_plan(
        context_path=args.context,
        legacy_report_path=args.legacy_report,
        serverless_report_path=args.serverless_report,
        output_directory=args.output_dir,
    )
    print(
        json.dumps(
            {
                "plan_id": plan["plan_id"],
                "plan_path": str(plan_path),
                "operation": plan["operation"],
                "mutating": False,
                "approval_required": plan["approval_required"],
                "current_backend": plan["production"][
                    "current_backend"
                ],
                "desired_backend": plan["production"][
                    "desired_backend"
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )


def cmd_deploy_lifecycle_plan(args):
    """Create a runtime park or resume plan without contacting AWS."""

    from src.gateway.deployment.runtime_lifecycle import (
        create_runtime_lifecycle_plan,
    )

    plan, plan_path = create_runtime_lifecycle_plan(
        context_path=args.context,
        output_directory=args.output_dir,
    )
    print(
        json.dumps(
            {
                "plan_id": plan["plan_id"],
                "plan_path": str(plan_path),
                "operation": plan["operation"],
                "mutating": False,
                "approval_required": plan["approval_required"],
                "current_state": plan["current_state"],
                "desired_state": plan["desired_state"],
            },
            indent=2,
            sort_keys=True,
        )
    )


def cmd_deploy_lifecycle_receipt(args):
    """Verify a completed lifecycle operation without contacting AWS."""

    from src.gateway.deployment.runtime_lifecycle_status import (
        create_runtime_lifecycle_receipt,
    )

    receipt, receipt_path = create_runtime_lifecycle_receipt(
        plan_path=args.plan,
        status_path=args.status,
        output_directory=args.output_dir,
    )
    print(
        json.dumps(
            {
                "receipt_id": receipt["receipt_id"],
                "receipt_path": str(receipt_path),
                "operation": receipt["operation"],
                "final_state": receipt["final_state"],
                "verified": True,
            },
            indent=2,
            sort_keys=True,
        )
    )


def cmd_deploy_standalone_plan(args):
    """Create a standalone ECS plan without contacting AWS."""

    from src.gateway.deployment.standalone_recipe import (
        create_standalone_ecs_plan,
    )

    plan, plan_path, task_path = create_standalone_ecs_plan(
        context_path=args.context,
        output_directory=args.output_dir,
    )
    print(
        json.dumps(
            {
                "plan_id": plan["plan_id"],
                "plan_path": str(plan_path),
                "task_definition_path": str(task_path),
                "mutating": False,
                "approval_required": plan["approval_required"],
                "created_network_resources": plan["ownership"][
                    "created_network_resources"
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )


def main():
    parser = argparse.ArgumentParser(
        prog="axon",
        description="AxonLLM — The neural control plane for enterprise LLMs",
    )
    sub = parser.add_subparsers(dest="command")

    from src.gateway.agentcore_setup import add_setup_subcommands

    add_setup_subcommands(sub)

    # deploy
    p_deploy = sub.add_parser(
        "deploy",
        help="Plan AxonLLM deployment lifecycle operations",
    )
    deploy_sub = p_deploy.add_subparsers(dest="deploy_command", required=True)
    p_deploy_plan = deploy_sub.add_parser(
        "plan",
        help="Write a deterministic, non-mutating deployment plan",
    )
    p_deploy_plan.add_argument(
        "--config",
        required=True,
        help="Path to a deployment configuration YAML file",
    )
    p_deploy_plan.add_argument(
        "--context",
        required=True,
        help="Path to an explicit non-secret planning context JSON file",
    )
    p_deploy_plan.add_argument(
        "--output-dir",
        default=".axon/plans",
        help="Directory for content-addressed plan artifacts",
    )
    p_deploy_edge_plan = deploy_sub.add_parser(
        "edge-plan",
        help=(
            "Write a qualified, non-mutating control-plane edge "
            "transition plan"
        ),
    )
    p_deploy_edge_plan.add_argument(
        "--context",
        required=True,
        help="Path to an explicit non-secret edge transition context",
    )
    p_deploy_edge_plan.add_argument(
        "--legacy-report",
        required=True,
        help="Passing Fargate production-validation report",
    )
    p_deploy_edge_plan.add_argument(
        "--serverless-report",
        required=True,
        help="Passing serverless-control validation report",
    )
    p_deploy_edge_plan.add_argument(
        "--output-dir",
        default=".axon/plans",
        help="Directory for content-addressed edge-plan artifacts",
    )
    p_deploy_lifecycle_plan = deploy_sub.add_parser(
        "lifecycle-plan",
        help=(
            "Write a non-mutating AgentCore runtime park or resume plan"
        ),
    )
    p_deploy_lifecycle_plan.add_argument(
        "--context",
        required=True,
        help="Path to an explicit non-secret runtime lifecycle context",
    )
    p_deploy_lifecycle_plan.add_argument(
        "--output-dir",
        default=".axon/plans",
        help="Directory for content-addressed lifecycle-plan artifacts",
    )
    p_deploy_lifecycle_receipt = deploy_sub.add_parser(
        "lifecycle-receipt",
        help=(
            "Verify observed park or resume state and write a receipt"
        ),
    )
    p_deploy_lifecycle_receipt.add_argument(
        "--plan",
        required=True,
        help="Path to the reviewed runtime lifecycle plan",
    )
    p_deploy_lifecycle_receipt.add_argument(
        "--status",
        required=True,
        help="Path to explicit non-secret post-operation observations",
    )
    p_deploy_lifecycle_receipt.add_argument(
        "--output-dir",
        default=".axon/receipts",
        help="Directory for content-addressed lifecycle receipts",
    )
    p_deploy_standalone_plan = deploy_sub.add_parser(
        "standalone-plan",
        help=(
            "Write a non-mutating standalone ECS plan for existing "
            "infrastructure"
        ),
    )
    p_deploy_standalone_plan.add_argument(
        "--context",
        required=True,
        help="Path to an explicit non-secret standalone ECS context",
    )
    p_deploy_standalone_plan.add_argument(
        "--output-dir",
        default=".axon/plans",
        help="Directory for content-addressed standalone plan artifacts",
    )

    # demo
    sub.add_parser("demo", help="Start server + generate real traffic for a live demo")

    # serve
    p_serve = sub.add_parser("serve", help="Start the AxonLLM gateway server")
    p_serve.add_argument("--demo-data", action="store_true", default=True, help="Load demo seed data (default: true)")
    p_serve.add_argument("--no-demo-data", dest="demo_data", action="store_false")

    # issue-key
    p_key = sub.add_parser("issue-key", help="Mint an API key (in-process; works under ENFORCE)")
    p_key.add_argument("-P", "--project", default="default", help="Project ID to scope the key to")
    p_key.add_argument(
        "-T",
        "--tenant",
        help=(
            "Tenant ID for a canonical service key. The default scopes become "
            "model.list,inference.invoke,query.select."
        ),
    )
    p_key.add_argument("-n", "--name", default="cli-issued", help="Human-readable key name")
    p_key.add_argument(
        "-s", "--scopes",
        help="Comma-separated scopes (default: chat, or canonical read/invoke "
             "scopes with --tenant). Admin scopes take an "
             "optional access level: 'admin:quotas:read' for read-only, "
             "'admin:quotas:write' or bare 'admin:quotas' for both, "
             "'admin:*' for everything, 'admin:*:read' to read everything",
    )

    # canonical tenant bootstrap
    p_bootstrap = sub.add_parser(
        "bootstrap-tenant",
        help="Create or verify a canonical tenant, project, and first administrator",
    )
    p_bootstrap.add_argument("-T", "--tenant", required=True)
    p_bootstrap.add_argument("-P", "--project", required=True)
    p_bootstrap.add_argument("--project-name")
    p_bootstrap.add_argument("--issuer", required=True)
    p_bootstrap.add_argument("--subject", required=True)
    p_bootstrap.add_argument("--user-name", required=True)
    p_bootstrap.add_argument("--display-name", default="")
    p_bootstrap.add_argument("--email")
    p_bootstrap.add_argument("--budget-limit", type=float)

    # chat
    p_chat = sub.add_parser("chat", help="Send a chat message")
    p_chat.add_argument("message", nargs="+", help="The message to send")
    p_chat.add_argument("-m", "--model", default="claude-sonnet", help="Model to use")
    p_chat.add_argument("-p", "--port", type=int, default=8000)
    p_chat.add_argument("-k", "--api-key", default=None, help="API key (or set AXON_API_KEY)")

    # models
    p_models = sub.add_parser("models", help="List available models")
    p_models.add_argument("-p", "--port", type=int, default=8000)
    p_models.add_argument("-k", "--api-key", default=None, help="API key (or set AXON_API_KEY)")

    args = parser.parse_args()

    try:
        if args.command == "setup" and args.setup_target == "agentcore":
            from src.gateway.agentcore_setup import cmd_setup_agentcore

            cmd_setup_agentcore(args)
        elif args.command == "setup" and args.setup_target == "local-demo":
            from src.gateway.agentcore_setup import cmd_setup_local_demo

            cmd_setup_local_demo(args)
        elif args.command == "demo":
            cmd_demo(args)
        elif args.command == "serve":
            cmd_serve(args)
        elif args.command == "issue-key":
            cmd_issue_key(args)
        elif args.command == "bootstrap-tenant":
            cmd_bootstrap_tenant(args)
        elif args.command == "chat":
            cmd_chat(args)
        elif args.command == "models":
            cmd_models(args)
        elif args.command == "deploy" and args.deploy_command == "plan":
            cmd_deploy_plan(args)
        elif (
            args.command == "deploy"
            and args.deploy_command == "edge-plan"
        ):
            cmd_deploy_edge_plan(args)
        elif (
            args.command == "deploy"
            and args.deploy_command == "lifecycle-plan"
        ):
            cmd_deploy_lifecycle_plan(args)
        elif (
            args.command == "deploy"
            and args.deploy_command == "lifecycle-receipt"
        ):
            cmd_deploy_lifecycle_receipt(args)
        elif (
            args.command == "deploy"
            and args.deploy_command == "standalone-plan"
        ):
            cmd_deploy_standalone_plan(args)
        else:
            parser.print_help()
    except ValueError as exc:
        parser.error(str(exc))


def demo():
    """Direct entry point for `uv run axon-demo`."""
    cmd_demo(None)


if __name__ == "__main__":
    main()
