"""Capture a Mac source-only Director preview for a reproducible Hook A/B.

Uses the normal preview worker and configured provider. Optional response
replays are labelled explicitly; they never count as new model observations.
Video, credentials, user settings and licensing logic are not modified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "app"), str(ROOT / "web_client")]

import commercial_analyzer as analyzer
import run_m3_new_golden_plan_fidelity as runner
import server


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-info", type=Path, required=True, help="Existing source_info.json; captures video, SRT and run controls")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--arm", required=True, help="A new run directory name; existing evidence is never overwritten")
    replay = parser.add_mutually_exclusive_group()
    replay.add_argument("--story-from", type=Path, help="Freeze this earlier run's real M1; make one new Casting call")
    replay.add_argument("--responses-from", type=Path, help="Replay both earlier real responses for local validation; no provider calls")
    args = parser.parse_args()
    base = args.output_root.resolve()
    out = base / args.arm
    out.mkdir(parents=True, exist_ok=False)
    info = json.loads(args.source_info.read_text(encoding="utf-8"))
    video, srt = Path(info["video"]), Path(info["srt"])
    source = base / "source"
    source.mkdir(exist_ok=True)
    manifest = {}
    for path in (video, srt, srt.with_suffix(".words.json"), srt.with_suffix(".asr_quality.json")):
        manifest[str(path)] = {"bytes": path.stat().st_size, "sha256": sha(path)}
        if path != video:
            dest = source / path.name
            if dest.exists():
                if sha(dest) != sha(path):
                    raise ValueError(f"Input changed between arms: {path.name}")
            else:
                shutil.copy2(path, dest)
    save(out / "inputs.json", manifest)
    settings = server._load_settings()
    save(out / "environment.json", {
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "working_diff_sha256": hashlib.sha256(subprocess.check_output(["git", "diff", "HEAD"], cwd=ROOT)).hexdigest(),
        "model": settings.get("model"), "base_url": settings.get("base_url"),
        "platform": sys.platform, "python": sys.version,
    })
    controls = dict(info.get("director_controls") or {})
    if "task_content_policy" in info:
        controls["content_policy"] = info["task_content_policy"]
    payload = server.SmartCutPayload(
        video_paths=[str(video)], srt_path=str(source / srt.name),
        target_duration=info["target_duration"], duration_tolerance=info.get("duration_tolerance"),
        versions=1, dedup_preset=info.get("dedup_preset", "medium"),
        video=info.get("video_options", {}), ai_controls=controls,
    )
    save(out / "payload.json", payload.model_dump())
    # The application ledger may include older runs with the same task ID.
    # Capture only this arm's actual provider exchanges, including runtime
    # options applied below _post_two_pass_director_request and failed calls.
    record_call = analyzer.record_ai_call
    exchanges = []

    def capture_exchange(**kwargs):
        exchanges.append({key: kwargs.get(key) for key in (
            "stage", "model", "request_started_at", "request_payload",
            "response_payload", "success", "error_type",
        )})
        save(out / "provider_exchanges.json", exchanges)
        return record_call(**kwargs)

    analyzer.record_ai_call = capture_exchange
    # Observe the real request. Do not persist Authorization or the API key.
    request = analyzer._post_two_pass_director_request

    def capture_request(**kwargs):
        stage = kwargs["stage"]
        save(out / f"{stage}.request.json", {key: value for key, value in kwargs.items() if key not in {"api_key", "response_hook"}})
        frozen = args.responses_from or (args.story_from if stage == "Director_story_contract" else None)
        if frozen:
            label = {"Director_story_contract": "story", "Director_beat_casting": "casting"}[stage]
            paths = list((frozen / "preview").glob(f"*.director_{label}.response.txt"))
            if len(paths) != 1:
                raise ValueError(f"Expected one real {label} response in {frozen}")
            save(out / f"{label}_replay.json", {"source": str(paths[0].resolve()), "sha256": sha(paths[0]), "network_call": False})
            return paths[0].read_text(encoding="utf-8")
        return request(**kwargs)

    analyzer._post_two_pass_director_request = capture_request
    run_case = runner._run_case

    def capture_case(*args, **kwargs):
        case = run_case(*args, **kwargs)
        save(out / "case.json", case)
        return case

    runner._run_case = capture_case
    ledger = runner._asset_ledger_for_source

    def capture_ledger(*args, **kwargs):
        result = ledger(*args, **kwargs)
        save(out / "candidate_ledger.json", result)
        return result

    runner._asset_ledger_for_source = capture_ledger
    server._commerce_director_workspace_root = lambda: out
    # Native review-video caches are keyed by preview ID, across experiments.
    preview_id = "hook-ab-" + hashlib.sha256(str(out).encode()).hexdigest()[:12] + "-" + args.arm
    server._run_commerce_director_preview_auto_batch("preview", preview_id, payload)
    preview = server._CLIP_PREVIEWS.get(preview_id, {})
    save(out / "editable_preview.json", preview)
    save(out / "task.json", server._TASKS.get("preview", {}))
    print(json.dumps({"arm": args.arm, "status": preview.get("status"), "message": preview.get("message")}, ensure_ascii=False))
    if preview.get("status") != "ready":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
