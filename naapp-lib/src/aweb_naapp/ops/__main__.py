"""Command line for a naapp's Render production operations.

    python -m aweb_naapp.ops [--config FILE] [--env-file FILE] COMMAND

status and verify only read. deploy, rollback, suspend, resume and job mutate the
service and require --apply plus the exact service ID. job runs only a job listed
in the app's ops/one-off-jobs.json, with each typed slot given as slot=value.
Each prints one JSON result; job exits 3 unless the job succeeded.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from aweb_naapp.ops import render
from aweb_naapp.ops.jobs import JobAllowlist, parse_slot_args

MUTATIONS = {"deploy", "rollback", "suspend", "resume", "job"}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m aweb_naapp.ops", description=__doc__)
    p.add_argument("--config", default=os.environ.get("PROD_CONFIG", "ops/render-production.json"))
    p.add_argument("--env-file", default=os.environ.get("RENDER_ENV_FILE", "~/.aweb-render/env"))
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    deploy = sub.add_parser("deploy")
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
    verify.add_argument("--deploy-id", default=os.environ.get("PROD_DEPLOY_ID", ""))
    verify.add_argument("--commit", default=os.environ.get("PROD_COMMIT", ""))
    rollback = sub.add_parser("rollback")
    rollback.add_argument("--rollback-deploy-id", default=os.environ.get("ROLLBACK_DEPLOY_ID", ""))
    rollback.add_argument("--rollback-commit", default=os.environ.get("ROLLBACK_COMMIT", ""))
    rollback.add_argument("--current-deploy-id", default=os.environ.get("CURRENT_DEPLOY_ID", ""))
    rollback.add_argument("--current-commit", default=os.environ.get("CURRENT_COMMIT", ""))
    rollback.add_argument("--confirm-service-id", default=os.environ.get("CONFIRM_SERVICE_ID", ""))
    rollback.add_argument("--timeout", type=int, default=900)
    rollback.add_argument("--apply", action="store_true", default=os.environ.get("APPLY") == "1")
    suspend = sub.add_parser("suspend")
    suspend.add_argument("--timeout", type=int, default=300)
    resume = sub.add_parser("resume")
    resume.add_argument("--commit", default=os.environ.get("PROD_COMMIT", ""))
    resume.add_argument("--timeout", type=int, default=900)
    job = sub.add_parser("job")
    job.add_argument("--jobs-file", default=os.environ.get("JOBS_FILE", "ops/one-off-jobs.json"))
    job.add_argument("--name", default=os.environ.get("JOB_NAME", ""))
    job.add_argument("--args", default=os.environ.get("JOB_ARGS", ""))
    job.add_argument("--timeout", type=int, default=600)
    for command in (suspend, resume, job):
        command.add_argument("--confirm-service-id", default=os.environ.get("CONFIRM_SERVICE_ID", ""))
        command.add_argument("--apply", action="store_true", default=os.environ.get("APPLY") == "1")
    return p


def run(args: argparse.Namespace) -> dict:
    config = render.ProductionConfig.load(Path(args.config))
    if args.command in MUTATIONS:
        render.confirm_apply(config, apply=args.apply, confirm_service_id=args.confirm_service_id)
    if args.command == "deploy":
        render.confirm_repository_change(args.from_repository, args.confirm_from_repository)
    if args.command == "resume":
        render.require_commit(args.commit)
    if args.command == "job":
        argv = JobAllowlist.load(Path(args.jobs_file)).argv(args.name, parse_slot_args(args.args))
    if getattr(args, "timeout", 1) <= 0:
        raise render.OpsError("timeout must be positive")
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
        )
    if args.command == "wait":
        return render.wait(
            client, config, deploy_id=args.deploy_id, commit=args.commit, timeout_seconds=args.timeout
        )
    if args.command == "verify":
        return render.verify(client, config, deploy_id=args.deploy_id, commit=args.commit)
    if args.command == "suspend":
        return render.suspend(client, config, timeout_seconds=args.timeout)
    if args.command == "resume":
        return render.resume(client, config, commit=args.commit, timeout_seconds=args.timeout)
    if args.command == "job":
        return render.run_job(client, config, argv, timeout_seconds=args.timeout)
    return render.rollback(
        client,
        config,
        rollback_deploy_id=args.rollback_deploy_id,
        rollback_commit=args.rollback_commit,
        current_deploy_id=args.current_deploy_id,
        current_commit=args.current_commit,
        timeout_seconds=args.timeout,
    )


def exit_status(result: dict) -> int:
    """0, except for a one-off job whose outcome is anything but succeeded."""
    return 0 if result.get("outcome", "succeeded") == "succeeded" else 3


def main() -> int:
    args = parser().parse_args()
    try:
        result = run(args)
    except render.OpsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return exit_status(result)


if __name__ == "__main__":
    raise SystemExit(main())
