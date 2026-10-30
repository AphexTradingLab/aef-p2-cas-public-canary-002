"""One-run, synthetic GitHub ref race for the public CAS canary."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

if __package__:
    from .cas_model import (
        canonical_bytes,
        seed_state,
        state_digest,
        successor_state,
        validate_seed,
        validate_successor,
    )
else:
    from cas_model import (
        canonical_bytes,
        seed_state,
        state_digest,
        successor_state,
        validate_seed,
        validate_successor,
    )

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
WRITERS = ("writer-a", "writer-b")
BRANCH = "cas-synthetic-001"


def git(*args: str, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args], text=True, capture_output=True, check=False, env=env
    )
    if check and proc.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc


def parse_time(value: str) -> float:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def is_stale_writer_rejection(
    output: str,
    *,
    expected_old_sha: str,
    observed_remote_sha: str,
    competing_sibling_verified: bool,
) -> bool:
    """Recognize a stale rejection only after exact ref and sibling readback checks."""
    if not SHA_RE.fullmatch(expected_old_sha) or not SHA_RE.fullmatch(observed_remote_sha):
        return False
    if observed_remote_sha == expected_old_sha or not competing_sibling_verified:
        return False
    exact_ref = re.escape(f"refs/heads/{BRANCH}")
    match = re.search(
        rf"cannot lock ref '{exact_ref}': is at (?P<observed>[0-9a-f]{{40}}) "
        rf"but expected (?P<expected>[0-9a-f]{{40}})",
        output,
        re.IGNORECASE,
    )
    if match:
        return (
            match.group("expected") == expected_old_sha
            and match.group("observed") == observed_remote_sha
        )
    # Some Git servers use the standard non-fast-forward wording instead of
    # exposing ref-lock SHAs. The exact push target is fixed in race(), and a
    # readback-verified incompatible sibling is required before classifying it.
    return any(
        phrase in output.lower()
        for phrase in ("non-fast-forward", "fetch first", "stale info")
    )


def write_outputs(values: dict[str, str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        raise RuntimeError("GITHUB_OUTPUT is unavailable")
    with open(output_path, "a", encoding="utf-8", newline="\n") as stream:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ValueError(f"multiline output rejected: {key}")
            stream.write(f"{key}={value}\n")


def api_jobs(repo: str, run_id: str, token: str) -> list[dict[str, Any]]:
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "synthetic-p2-cas-canary",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"GitHub Actions jobs read failed: {type(exc).__name__}") from exc
    jobs = result.get("jobs")
    if not isinstance(jobs, list):
        raise RuntimeError("jobs API response has no jobs list")
    return jobs


def one_job(jobs: list[dict[str, Any]], name: str) -> dict[str, Any]:
    found = [job for job in jobs if job.get("name") == name]
    if len(found) != 1:
        raise RuntimeError(f"expected one API job named {name}; got {len(found)}")
    return found[0]


def one_step(job: dict[str, Any], name: str) -> dict[str, Any]:
    found = [step for step in job.get("steps", []) if step.get("name") == name]
    if len(found) != 1:
        raise RuntimeError(f"expected one step {name} in {job.get('name')}; got {len(found)}")
    return found[0]


def prepare(args: argparse.Namespace) -> None:
    if not SHA_RE.fullmatch(args.base_sha):
        raise ValueError("base_sha must be a full lowercase 40-character SHA")
    if args.writer not in WRITERS or not args.run_id.isdigit():
        raise ValueError("invalid writer or run id")
    status = git("status", "--porcelain").stdout
    if status:
        raise RuntimeError("checkout is not clean before candidate preparation")
    remote = git("remote", "get-url", "origin").stdout.strip()
    expected_remote = f"https://github.com/{args.repo}.git"
    if remote.rstrip("/") != expected_remote.rstrip("/"):
        raise RuntimeError("origin is not the public canary repository")
    git("fetch", "--no-tags", "origin", f"refs/heads/{BRANCH}")
    observed_tip = git("rev-parse", "FETCH_HEAD").stdout.strip()
    if observed_tip != args.base_sha:
        raise RuntimeError("synthetic branch no longer equals the supplied immutable base")
    raw_base = git("show", f"{args.base_sha}:state.json").stdout
    base = json.loads(raw_base)
    validate_seed(base)
    digest = state_digest(base)
    git("switch", "--detach", args.base_sha)
    candidate = successor_state(base, args.run_id, args.writer)
    Path("state.json").write_bytes(canonical_bytes(candidate) + b"\n")
    git("add", "--", "state.json")
    paths = git("diff", "--cached", "--name-only").stdout.splitlines()
    if paths != ["state.json"]:
        raise RuntimeError(f"candidate staged unexpected paths: {paths}")
    git(
        "-c", "user.name=Synthetic CAS Writer",
        "-c", f"user.email={args.writer}@users.noreply.github.com",
        "commit", "-m", f"synthetic CAS successor: {args.writer}",
    )
    candidate_sha = git("rev-parse", "HEAD").stdout.strip()
    parent = git("rev-parse", "HEAD^").stdout.strip()
    if parent != args.base_sha:
        raise RuntimeError("candidate is not a sibling with the required immutable parent")
    candidate_from_git = json.loads(git("show", f"{candidate_sha}:state.json").stdout)
    validate_successor(base, candidate_from_git)
    values = {
        "candidate_sha": candidate_sha,
        "parent_sha": parent,
        "base_digest": digest,
        "candidate_state_sha256": state_digest(candidate_from_git),
        "operation_id": candidate["operation_id"],
        "event_id": candidate["event_id"],
        "base_sha": args.base_sha,
        "revision": "0",
    }
    write_outputs(values)
    print(json.dumps({"phase": "PREPARED", "writer": args.writer, **values}, sort_keys=True))


def create_seed(args: argparse.Namespace) -> None:
    value = seed_state()
    validate_seed(value)
    Path(args.path).write_bytes(canonical_bytes(value) + b"\n")
    print(json.dumps({"phase": "SEED_CREATED", "state_sha256": state_digest(value)}, sort_keys=True))


def askpass_context(token: str) -> tuple[tempfile.TemporaryDirectory[str], dict[str, str]]:
    temp = tempfile.TemporaryDirectory(prefix="p2-cas-askpass-")
    helper = Path(temp.name) / "askpass.sh"
    helper.write_text(
        "#!/bin/sh\n"
        "prompt=$(printf '%s' \"$1\" | tr '[:upper:]' '[:lower:]')\n"
        "case \"$prompt\" in\n"
        "  *username*) printf '%s\\n' 'x-access-token' ;;\n"
        "  *password*) printf '%s\\n' \"$GH_TOKEN\" ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    os.chmod(helper, 0o700)
    env = os.environ.copy()
    env.update(
        {
            "GH_TOKEN": token,
            "GIT_ASKPASS": str(helper),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        }
    )
    return temp, env


def wait_for_race_window(args: argparse.Namespace, token: str) -> tuple[dict[str, Any], float]:
    deadline = time.monotonic() + 105
    while time.monotonic() < deadline:
        jobs = api_jobs(args.repo, args.run_id, token)
        a_matches = [job for job in jobs if job.get("name") == "writer-a"]
        b_matches = [job for job in jobs if job.get("name") == "writer-b"]
        if len(a_matches) == len(b_matches) == 1:
            a, b = a_matches[0], b_matches[0]
            current_matches = [job for job in jobs if job.get("name") == args.writer]
            if (
                len(current_matches) == 1
                and a.get("status") == b.get("status") == "in_progress"
                and a.get("runner_id") is not None
                and b.get("runner_id") is not None
                and a["runner_id"] != b["runner_id"]
            ):
                try:
                    a_prepare = one_step(a, "Prepare sibling commit")
                    b_prepare = one_step(b, "Prepare sibling commit")
                    a_race = one_step(a, "Race push window")
                    b_race = one_step(b, "Race push window")
                except RuntimeError:
                    time.sleep(0.5)
                    continue
                if (
                    a_prepare.get("conclusion") == "success"
                    and b_prepare.get("conclusion") == "success"
                    and a_race.get("status") == "in_progress"
                    and b_race.get("status") == "in_progress"
                ):
                    latest_start = max(
                        parse_time(a_race["started_at"]),
                        parse_time(b_race["started_at"]),
                    )
                    release = latest_start + 6.0
                    if time.time() > release + 0.5:
                        raise RuntimeError("shared release instant was missed; refusing late push")
                    return current_matches[0], release
        time.sleep(0.5)
    raise RuntimeError("writers did not reach a shared ready window before timeout")


def race(args: argparse.Namespace) -> None:
    token = os.environ.get("GH_TOKEN", "")
    if (
        not token
        or not SHA_RE.fullmatch(args.base_sha)
        or not SHA_RE.fullmatch(args.parent_sha)
        or not SHA_RE.fullmatch(args.candidate_sha)
        or not DIGEST_RE.fullmatch(args.base_digest)
        or not DIGEST_RE.fullmatch(args.candidate_state_sha256)
    ):
        raise ValueError("missing scoped token or invalid SHA")
    expected_remote = f"https://github.com/{args.repo}.git"
    if git("remote", "get-url", "origin").stdout.strip().rstrip("/") != expected_remote.rstrip("/"):
        raise RuntimeError("origin is not the expected public canary repository")
    if args.parent_sha != args.base_sha or git("rev-parse", f"{args.candidate_sha}^").stdout.strip() != args.base_sha:
        raise RuntimeError("candidate parent is not the immutable base")
    base = json.loads(git("show", f"{args.base_sha}:state.json").stdout)
    candidate = json.loads(git("show", f"{args.candidate_sha}:state.json").stdout)
    validate_successor(base, candidate)
    if candidate["writer_id"] != args.writer:
        raise RuntimeError("candidate identity does not match this writer")
    if candidate["operation_id"] != args.operation_id or candidate["event_id"] != args.event_id:
        raise RuntimeError("candidate operation/event identity changed")
    if state_digest(base) != args.base_digest or state_digest(candidate) != args.candidate_state_sha256:
        raise RuntimeError("candidate or base digest changed after preparation")
    job, release = wait_for_race_window(args, token)
    while time.time() < release:
        time.sleep(min(0.05, max(0.0, release - time.time())))
    askpass, env = askpass_context(token)
    try:
        start = iso_now()
        result = git(
            "push", "--porcelain", "origin",
            f"{args.candidate_sha}:refs/heads/{BRANCH}",
            check=False,
            env=env,
        )
    finally:
        askpass.cleanup()
    end = iso_now()
    combined = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode == 0:
        outcome = "ACCEPTED"
    else:
        ref_readback = git("ls-remote", "--heads", "origin", f"refs/heads/{BRANCH}")
        ref_lines = [line.split("\t", 1) for line in ref_readback.stdout.splitlines() if "\t" in line]
        if len(ref_lines) != 1 or ref_lines[0][1] != f"refs/heads/{BRANCH}":
            raise RuntimeError("could not uniquely read back the CAS ref after rejected push")
        observed_remote_sha = ref_lines[0][0]
        git("fetch", "--no-tags", "origin", f"refs/heads/{BRANCH}")
        observed_commit = git("rev-parse", "FETCH_HEAD").stdout.strip()
        if observed_commit != observed_remote_sha:
            raise RuntimeError("CAS ref changed during rejected-push readback")
        observed_parents = git("rev-list", "--parents", "-n", "1", observed_commit).stdout.split()
        observed_state = json.loads(git("show", f"{observed_commit}:state.json").stdout)
        validate_successor(base, observed_state)
        competing_sibling_verified = (
            len(observed_parents) == 2
            and observed_parents[1] == args.base_sha
            and observed_commit != args.candidate_sha
            and observed_state.get("writer_id") in WRITERS
            and observed_state.get("writer_id") != args.writer
            and observed_state.get("operation_id") != args.operation_id
            and observed_state.get("event_id") != args.event_id
        )
        if is_stale_writer_rejection(
            combined,
            expected_old_sha=args.base_sha,
            observed_remote_sha=observed_remote_sha,
            competing_sibling_verified=competing_sibling_verified,
        ):
            outcome = "STALE_WRITER"
        else:
            outcome = "ERROR"
    outputs = {
        "outcome": outcome,
        "push_start_utc": start,
        "push_end_utc": end,
        "runner_id": str(job["runner_id"]),
        "runner_name": str(job.get("runner_name", "")),
        "candidate_sha": args.candidate_sha,
        "base_sha": args.base_sha,
        "parent_sha": args.parent_sha,
        "base_digest": args.base_digest,
        "candidate_state_sha256": args.candidate_state_sha256,
        "operation_id": args.operation_id,
        "event_id": args.event_id,
        "push_attempts": "1",
        "push_return_code": str(result.returncode),
    }
    write_outputs(outputs)
    print(json.dumps({"phase": "PUSH_RESULT", "writer": args.writer, **outputs}, sort_keys=True))
    if combined:
        print("git-push-output:")
        print(combined)
    if outcome == "ERROR":
        raise RuntimeError("push failed for a reason other than the expected stale sibling rejection")


def reconcile() -> None:
    env = os.environ
    run_id = env.get("GITHUB_RUN_ID", "")
    repo = env.get("GITHUB_REPOSITORY", "")
    token = env.get("GH_TOKEN", "")
    base_sha = env.get("BASE_SHA", "")
    main_sha = env.get("MAIN_SHA", "")
    if not run_id.isdigit() or not token or not SHA_RE.fullmatch(base_sha) or not SHA_RE.fullmatch(main_sha):
        raise RuntimeError("reconciler inputs are invalid")
    writers: dict[str, dict[str, str]] = {}
    for key, writer in (("A", "writer-a"), ("B", "writer-b")):
        prefix = f"WRITER_{key}_"
        writers[writer] = {
            field.lower(): env.get(prefix + field, "")
            for field in (
                "OUTCOME", "PUSH_START_UTC", "PUSH_END_UTC", "RUNNER_ID",
                "RUNNER_NAME", "CANDIDATE_SHA", "BASE_SHA", "BASE_DIGEST",
                "PARENT_SHA", "CANDIDATE_STATE_SHA256", "OPERATION_ID",
                "EVENT_ID", "PUSH_ATTEMPTS", "PUSH_RETURN_CODE",
            )
        }
    a, b = writers["writer-a"], writers["writer-b"]
    if any(not value["candidate_sha"] or not value["outcome"] for value in writers.values()):
        raise RuntimeError("one or more writer jobs produced no result")
    if a["base_sha"] != b["base_sha"] or a["base_sha"] != base_sha:
        raise RuntimeError("writers did not use the same base SHA")
    if a["base_digest"] != b["base_digest"]:
        raise RuntimeError("writers did not read the same canonical base digest")
    if a["parent_sha"] != b["parent_sha"] or a["parent_sha"] != base_sha:
        raise RuntimeError("writer candidates are not siblings with the same parent")
    if a["candidate_state_sha256"] == b["candidate_state_sha256"]:
        raise RuntimeError("writer successor states are not mutually distinct")
    if a["operation_id"] == b["operation_id"] or a["event_id"] == b["event_id"]:
        raise RuntimeError("writer operation identities are not distinct")
    if a["runner_id"] == b["runner_id"]:
        raise RuntimeError("writer runner identities are not independent")
    if {a["outcome"], b["outcome"]} != {"ACCEPTED", "STALE_WRITER"}:
        raise RuntimeError("writer outcomes are not exactly one accepted and one stale")
    if a["push_attempts"] != "1" or b["push_attempts"] != "1":
        raise RuntimeError("a writer did not make exactly one push attempt")
    if a["candidate_sha"] == b["candidate_sha"]:
        raise RuntimeError("sibling candidate commits are not distinct")
    starts = {writer: parse_time(value["push_start_utc"]) for writer, value in writers.items()}
    ends = {writer: parse_time(value["push_end_utc"]) for writer, value in writers.items()}
    push_overlap = max(starts.values()) < min(ends.values())
    if not push_overlap:
        raise RuntimeError("writer push intervals did not overlap; concurrency is unverified")
    jobs = api_jobs(repo, run_id, token)
    job_names = ("writer-a", "writer-b", "reconciler")
    run_jobs = [one_job(jobs, name) for name in job_names]
    runner_ids = [str(job.get("runner_id", "")) for job in run_jobs]
    if any(not value for value in runner_ids) or len(set(runner_ids)) != 3:
        raise RuntimeError("GitHub job readback does not prove three independent runners")
    if any(job.get("conclusion") != "success" for job in run_jobs[:2]):
        raise RuntimeError("a writer job did not complete successfully")
    for job, writer in zip(run_jobs[:2], WRITERS):
        if str(job.get("runner_id")) != writers[writer]["runner_id"]:
            raise RuntimeError(f"{writer} runner id output disagrees with GitHub job record")
        if "ubuntu-24.04" not in job.get("labels", []):
            raise RuntimeError(f"{writer} did not use the requested standard runner label")
    api_race_steps = [one_step(job, "Race push window") for job in run_jobs[:2]]
    api_step_starts = [parse_time(step["started_at"]) for step in api_race_steps]
    api_step_ends = [parse_time(step["completed_at"]) for step in api_race_steps]
    api_race_window_overlap = max(api_step_starts) < min(api_step_ends)
    if not api_race_window_overlap:
        raise RuntimeError("GitHub job timestamps do not prove overlapping writer race windows")
    accepted_writer = next(name for name, value in writers.items() if value["outcome"] == "ACCEPTED")
    winner = writers[accepted_writer]
    git("fetch", "--no-tags", "origin", f"refs/heads/{BRANCH}")
    tip = git("rev-parse", "FETCH_HEAD").stdout.strip()
    if tip != winner["candidate_sha"]:
        raise RuntimeError("readback branch tip is not the accepted writer's candidate")
    git("cat-file", "-e", f"{base_sha}^{{commit}}")
    parents = git("rev-list", "--parents", "-n", "1", tip).stdout.split()
    if len(parents) != 2 or parents[1] != base_sha:
        raise RuntimeError("remote winner is not exactly one direct successor of the shared base")
    changed = git("diff-tree", "--no-commit-id", "--name-only", "-r", base_sha, tip).stdout.splitlines()
    if changed != ["state.json"]:
        raise RuntimeError(f"remote winner changed unexpected paths: {changed}")
    base = json.loads(git("show", f"{base_sha}:state.json").stdout)
    winner_state = json.loads(git("show", f"{tip}:state.json").stdout)
    validate_seed(base)
    validate_successor(base, winner_state)
    if winner_state["writer_id"] != accepted_writer:
        raise RuntimeError("durable winner state does not match accepted writer")
    if winner_state["operation_id"] != winner["operation_id"] or winner_state["event_id"] != winner["event_id"]:
        raise RuntimeError("durable winner identity differs from writer record")
    if state_digest(winner_state) != winner["candidate_state_sha256"]:
        raise RuntimeError("readback winner state digest differs from writer candidate")
    refs = git("ls-remote", "--heads", "origin").stdout.splitlines()
    ref_names = sorted(line.split("\t", 1)[1] for line in refs)
    if ref_names != ["refs/heads/cas-synthetic-001", "refs/heads/main"]:
        raise RuntimeError(f"unexpected remote branch refs: {ref_names}")
    if git("ls-remote", "--tags", "origin").stdout.strip():
        raise RuntimeError("unexpected tag refs exist in the synthetic repository")
    main_tip = next(line.split("\t", 1)[0] for line in refs if line.endswith("\trefs/heads/main"))
    if main_tip != main_sha:
        raise RuntimeError("main moved during the certification run")
    result = {
        "result": "P2_REMOTE_SYNTHETIC_PASS",
        "run_id": run_id,
        "repo": repo,
        "main_sha_unchanged": main_sha,
        "shared_base_sha": base_sha,
        "shared_base_revision": base["revision"],
        "shared_base_digest": state_digest(base),
        "writers": writers,
        "writer_runner_ids": runner_ids[:2],
        "reconciler_runner_id": runner_ids[2],
        "push_intervals_overlap": push_overlap,
        "github_server_race_step_intervals_overlap": api_race_window_overlap,
        "accepted_writer": accepted_writer,
        "winner_sha": tip,
        "winner_state_sha256": state_digest(winner_state),
        "winner_revision": winner_state["revision"],
        "durable_successor_count": 1,
        "changed_paths": changed,
        "remote_heads": ref_names,
        "authority_invariants": {
            key: winner_state[key]
            for key in (
                "authority", "execution_authorized", "dispatch_status",
                "outbox_write", "continuation_decision",
            )
        },
        "dispatches": 0,
        "external_writes_outside_canary_ref": 0,
    }
    print(json.dumps(result, sort_keys=True, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p_seed = sub.add_parser("seed")
    p_seed.add_argument("--path", default="state.json")
    p_seed.set_defaults(func=create_seed)
    p_prepare = sub.add_parser("prepare")
    p_prepare.add_argument("--repo", required=True)
    p_prepare.add_argument("--base-sha", required=True)
    p_prepare.add_argument("--run-id", required=True)
    p_prepare.add_argument("--writer", choices=WRITERS, required=True)
    p_prepare.set_defaults(func=prepare)
    p_race = sub.add_parser("race")
    p_race.add_argument("--repo", required=True)
    p_race.add_argument("--run-id", required=True)
    p_race.add_argument("--writer", choices=WRITERS, required=True)
    p_race.add_argument("--base-sha", required=True)
    p_race.add_argument("--parent-sha", required=True)
    p_race.add_argument("--base-digest", required=True)
    p_race.add_argument("--candidate-sha", required=True)
    p_race.add_argument("--candidate-state-sha256", required=True)
    p_race.add_argument("--operation-id", required=True)
    p_race.add_argument("--event-id", required=True)
    p_race.set_defaults(func=race)
    p_reconcile = sub.add_parser("reconcile")
    p_reconcile.set_defaults(func=lambda _: reconcile())
    args = parser.parse_args()
    try:
        args.func(args)
        return 0
    except Exception as exc:
        print(json.dumps({"result": "FAIL_CLOSED", "error_type": type(exc).__name__, "reason": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
