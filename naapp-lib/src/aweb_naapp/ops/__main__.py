"""Command line for a naapp's Render production operations.

    python -m aweb_naapp.ops [--config FILE] [--env-file FILE] COMMAND

status and verify only read. deploy and rollback mutate the service and require
--apply plus the exact service ID. Each prints one JSON result.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from aweb_naapp.ops import render
from aweb_naapp.ops.evidence import EvidenceError, HealthEvidenceRun, verifier_identity
from aweb_naapp.ops.git import run_git


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m aweb_naapp.ops", description=__doc__)
    p.add_argument("--config", default=os.environ.get("PROD_CONFIG", "ops/render-production.json"))
    p.add_argument("--env-file", default=os.environ.get("RENDER_ENV_FILE", "~/.aweb-render/env"))
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser(
        "public-identity", help="the running deploy as its public health reports it; no Render key"
    )
    deploy = sub.add_parser("deploy")
    deploy.add_argument("--evidence-dir", default=os.environ.get("PROD_EVIDENCE_DIR", ""))
    deploy.add_argument("--commit", default=os.environ.get("PROD_COMMIT", ""))
    deploy.add_argument("--rollback-deploy-id", default=os.environ.get("ROLLBACK_DEPLOY_ID", ""))
    deploy.add_argument("--rollback-commit", default=os.environ.get("ROLLBACK_COMMIT", ""))
    deploy.add_argument("--repo-root", default=".")
    # Only for the first deploy after the service moved repositories: the repository
    # it moved from, repeated in its own confirmation.
    deploy.add_argument("--from-repository", default=os.environ.get("FROM_REPOSITORY") or None)
    deploy.add_argument(
        "--confirm-from-repository", default=os.environ.get("CONFIRM_FROM_REPOSITORY", "")
    )
    deploy.add_argument("--confirm-service-id", default=os.environ.get("CONFIRM_SERVICE_ID", ""))
    deploy.add_argument("--timeout", type=int, default=900)
    deploy.add_argument("--apply", action="store_true", default=os.environ.get("APPLY") == "1")
    wait = sub.add_parser("wait")
    wait.add_argument("--deploy-id", default=os.environ.get("PROD_DEPLOY_ID", ""))
    wait.add_argument("--commit", default=os.environ.get("PROD_COMMIT", ""))
    wait.add_argument("--timeout", type=int, default=900)
    verify = sub.add_parser("verify")
    verify.add_argument("--evidence-dir", default=os.environ.get("PROD_EVIDENCE_DIR", ""))
    verify.add_argument("--deploy-id", default=os.environ.get("PROD_DEPLOY_ID", ""))
    verify.add_argument("--commit", default=os.environ.get("PROD_COMMIT", ""))
    rollback = sub.add_parser("rollback")
    rollback.add_argument("--evidence-dir", default=os.environ.get("PROD_EVIDENCE_DIR", ""))
    rollback.add_argument("--rollback-deploy-id", default=os.environ.get("ROLLBACK_DEPLOY_ID", ""))
    rollback.add_argument("--rollback-commit", default=os.environ.get("ROLLBACK_COMMIT", ""))
    rollback.add_argument("--current-deploy-id", default=os.environ.get("CURRENT_DEPLOY_ID", ""))
    rollback.add_argument("--current-commit", default=os.environ.get("CURRENT_COMMIT", ""))
    rollback.add_argument("--confirm-service-id", default=os.environ.get("CONFIRM_SERVICE_ID", ""))
    rollback.add_argument("--timeout", type=int, default=900)
    rollback.add_argument("--apply", action="store_true", default=os.environ.get("APPLY") == "1")
    return p


EVIDENCE_LABELS = {
    "deploy": "deploy-health",
    "verify": "verify-health",
    "rollback": "rollback-health",
}


def _evidence_run(
    args: argparse.Namespace, config: render.ProductionConfig
) -> HealthEvidenceRun | None:
    """The command's evidence directory (PROD_EVIDENCE_DIR), named before any Render
    call or key read, with the executing verifier's identity as its metadata."""
    if args.command not in EVIDENCE_LABELS:
        return None
    value = str(args.evidence_dir or "").strip()
    if not value:
        if config.evidence == "required":
            raise render.OpsError(
                f"PROD_EVIDENCE_DIR/--evidence-dir is required for {config.service_name}"
            )
        return None
    if not Path(value).is_absolute():
        raise render.OpsError("PROD_EVIDENCE_DIR/--evidence-dir must be an absolute path")
    script = Path(render.__file__).resolve()
    top = run_git(
        ["-C", str(script.parent), "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
    )
    if top.returncode != 0:
        raise render.OpsError("the ops tool must run from a checkout to keep evidence")
    root = Path(top.stdout.strip()).resolve()
    commit = getattr(args, "commit", "") or getattr(args, "rollback_commit", "")
    metadata = {
        **verifier_identity(repo_root=root, script_path=script),
        "config_sha256": config.source_sha256,
        "service_id": config.service_id,
        "expected_commit": str(commit),
    }
    return HealthEvidenceRun(
        Path(value),
        label=EVIDENCE_LABELS[args.command],
        schema=f"{config.app_id}.health-evidence.v1",
        repository_root=root,
        metadata=metadata,
    )


def run(args: argparse.Namespace) -> dict:
    config = render.ProductionConfig.load(Path(args.config))
    if args.command in {"deploy", "rollback"}:
        render.confirm_apply(config, apply=args.apply, confirm_service_id=args.confirm_service_id)
    if args.command == "deploy":
        render.confirm_repository_change(args.from_repository, args.confirm_from_repository)
    if args.command == "public-identity":
        return render.public_identity(config)
    if getattr(args, "timeout", 1) <= 0:
        raise render.OpsError("timeout must be positive")
    evidence = _evidence_run(args, config)
    client = render.RenderClient(render.load_api_key(Path(args.env_file).expanduser()))
    if args.command == "status":
        return render.status(client, config)
    if args.command == "deploy":
        return render.deploy(
            client,
            config,
            commit=args.commit,
            rollback_deploy_id=args.rollback_deploy_id,
            rollback_commit=args.rollback_commit,
            repo_root=Path(args.repo_root),
            from_repository=args.from_repository,
            timeout_seconds=args.timeout,
            evidence=evidence,
        )
    if args.command == "wait":
        return render.wait(
            client,
            config,
            deploy_id=args.deploy_id,
            commit=args.commit,
            timeout_seconds=args.timeout,
        )
    if args.command == "verify":
        return render.verify(
            client, config, deploy_id=args.deploy_id, commit=args.commit, evidence=evidence
        )
    return render.rollback(
        client,
        config,
        rollback_deploy_id=args.rollback_deploy_id,
        rollback_commit=args.rollback_commit,
        current_deploy_id=args.current_deploy_id,
        current_commit=args.current_commit,
        timeout_seconds=args.timeout,
        evidence=evidence,
    )


def main() -> int:
    args = parser().parse_args()
    try:
        result = run(args)
    except (render.OpsError, EvidenceError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
