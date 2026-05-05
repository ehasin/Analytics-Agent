"""
eval_runner.py — Batch validation runner for the analytics agent.

Log is built in memory. Summary is prepended at the top.
The file is written to disk AFTER the evaluation loop (including on failure),
so partial results always get saved.

Provides:
  - run_eval   Run named test sets → summary dict
"""

import datetime
import traceback
from pathlib import Path

from _agent.prompts import EVAL_SUMMARY_PROMPT


def run_eval(
    agent_fn,
    llm_fn,
    test_sets: dict[str, list[dict]],
    name: str = "Validation",
    logs_dir: str | Path | None = None,
    mode: int = 0,
) -> dict:
    """Run multiple named test sets through the agent.

    Log is accumulated in memory and written to disk at the end,
    even if the run crashes partway through.
    """
    total_questions = sum(len(qs) for qs in test_sets.values())
    total_passed = 0
    total_count = 0
    by_set = {}
    summary_rows = []       # (q_num, set_name, question_text, status, summary)
    log_body_parts = []     # strings — detail section, built progressively
    global_q_num = 0
    critical_error = None

    # ── Header (written to log later) ────────────────────
    started = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n  Model Validation: {', '.join(test_sets.keys())}")
    print(f"  {total_questions} questions across {len(test_sets)} set(s)")

    # ── Main evaluation loop ─────────────────────────────
    try:
        for set_name, questions in test_sets.items():
            set_passed = 0
            set_results = []

            print(f"\n{'=' * 60}")
            print(f"  {set_name} ({len(questions)} questions)")
            print(f"{'=' * 60}")

            log_body_parts.append(f"# {set_name}\n\n")

            for i, q in enumerate(questions):
                global_q_num += 1

                # ── Run agent ────────────────────────
                r = agent_fn(q["question"], mode=mode)

                # ── Classify ─────────────────────────
                if r.get("error"):
                    classification = "error"
                elif r.get("answer") and any(
                    x in r["answer"].lower() for x in ["can't answer", "cannot answer"]
                ):
                    classification = "cant_answer"
                elif r.get("answer") and any(
                    x in r["answer"].lower()
                    for x in ["unclear", "clarif", "please specify", "please clarify"]
                ):
                    classification = "clarifications_needed"
                elif r.get("answer"):
                    classification = "can_answer"
                else:
                    classification = "no_output"

                # ── Validate ─────────────────────────
                success = False
                if r.get("error"):
                    status = "ERROR"
                elif r.get("answer"):
                    success = q["validate"](r["answer"])
                    if not success and r.get("narrative"):
                        success = q["validate"](r["narrative"])
                    status = "PASS" if success else "FAIL"
                else:
                    status = "NO OUTPUT"

                # ── LLM assessment ───────────────────
                narrative = r.get("narrative", "(no narrative)")
                try:
                    summary = llm_fn(EVAL_SUMMARY_PROMPT.format(
                        question=q["question"],
                        expected=q["expected"],
                        narrative=narrative[:4000],
                        status=status,
                    ))
                except Exception:
                    summary = f"{status}: LLM summary unavailable."

                # ── Reconcile lambda with LLM assessment ─
                if success and any(x in summary.lower() for x in [
                    "failed entirely", "entirely unanswered", "complete failure",
                    "no partial credit", "could not be completed",
                ]):
                    success = False
                    status = "FAIL (override: assessment contradicts lambda)"

                if not success and status == "FAIL" and all(x not in summary.lower() for x in [
                    "fail", "incorrect", "missing", "incomplete", "error",
                    "did not", "could not", "unable", "unanswered",
                ]) and any(x in summary.lower() for x in [
                    "correctly", "well-structured", "accurate", "thorough",
                    "exceeded expectations", "fully satisfied",
                ]):
                    success = True
                    status = "PASS (override: assessment contradicts lambda)"
                    
                # ── Console output ───────────────────
                print(f"\n  Q{global_q_num}: {q['question']}")
                narr_preview = narrative[:300] + ("..." if len(narrative) > 300 else "")
                print(f"  Narrative: {narr_preview}")
                print(f"  Assessment: {summary}")
                print(f"  → {status}")

                # ── Append to log buffer ─────────────
                log_body_parts.append(
                    _format_question_log(global_q_num, q, r, classification, status, summary)
                )

                # ── Collect results ──────────────────
                set_passed += success
                summary_rows.append((global_q_num, set_name, q["question"], status, summary))
                set_results.append({
                    "question": q["question"],
                    "expected": q["expected"],
                    "classification": classification,
                    "status": status,
                    "summary": summary,
                    "code": r.get("code", ""),
                    "answer": r.get("answer"),
                    "queries": r.get("queries", []),
                    "narrative": narrative,
                    "mode": r.get("mode", mode),
                    "error": r.get("error"),
                })

            # Per-set summary
            print(f"\n  [{set_name}] {set_passed}/{len(questions)} passed")
            log_body_parts.append(
                f"**{set_name}: {set_passed}/{len(questions)} passed**\n\n---\n\n"
            )

            by_set[set_name] = {
                "passed": set_passed,
                "total": len(questions),
                "results": set_results,
            }
            total_passed += set_passed
            total_count += len(questions)

    except Exception as e:
        critical_error = traceback.format_exc()
        log_body_parts.append(
            f"\n## CRITICAL ERROR at Q{global_q_num}\n\n"
            f"```\n{critical_error}\n```\n\n---\n\n"
        )
        # Count what we have so far
        total_passed = sum(sv["passed"] for sv in by_set.values())
        total_count = sum(sv["total"] for sv in by_set.values())

    # ── Grand summary (console) ──────────────────────────
    completed = "INCOMPLETE — " if critical_error else ""
    print(f"\n{'=' * 60}")
    print(f"  {completed}{name}: {total_passed}/{total_count} passed")
    print(f"{'=' * 60}")

    print(f"\n  {'Set':<20} {'Question':<45} {'Result'}")
    print(f"  {'-' * 20} {'-' * 45} {'-' * 6}")
    for q_num, sn, q_text, st, smry in summary_rows:
        display_text = q_text[:42] + "..." if len(q_text) > 45 else q_text
        display_set = sn[:20]
        print(f"  {display_set:<20} {display_text:<45} {st}")
        # Assessment on next line, indented
        smry_preview = smry[:120] + ("..." if len(smry) > 120 else "")
        print(f"  {'':20} ↳ {smry_preview}")

    if critical_error:
        print(f"\n  ⚠ Run aborted due to critical error. See log for details.")

    # ── Write log to disk ────────────────────────────────
    log_path = None
    if logs_dir:
        log_path = _write_log_file(
            logs_dir=logs_dir, name=name, started=started, mode=mode,
            test_sets=test_sets, total_questions=total_questions,
            total_passed=total_passed, total_count=total_count,
            by_set=by_set, summary_rows=summary_rows,
            body_parts=log_body_parts, critical_error=critical_error,
        )

        from IPython.display import display, HTML
        link_html = _gdrive_link(log_path)
        display(HTML(link_html))

    return {
        "name": name,
        "total_passed": total_passed,
        "total_count": total_count,
        "by_set": by_set,
        "log_path": str(log_path) if log_path else None,
    }


# ── Log file assembly ────────────────────────────────────────

def _write_log_file(
    logs_dir, name, started, mode, test_sets, total_questions,
    total_passed, total_count, by_set, summary_rows, body_parts,
    critical_error,
):
    """Assemble the log: header → summary → detail body. Write once."""
    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    ts_file = started.replace("-", "_").replace(" ", "_").replace(":", "_")[:16]
    log_path = logs_dir / f"validation_log_{ts_file}.md"

    parts = []

    # Header
    parts.append(f"# {name}\n\n")
    parts.append(f"**Started:** {started}\n")
    parts.append(f"**Mode:** {mode}\n")
    parts.append(f"**Test sets:** {', '.join(test_sets.keys())}\n")
    parts.append(f"**Total cases:** {total_questions}\n")
    if critical_error:
        parts.append(f"**Status:** INCOMPLETE — aborted due to critical error\n")
    parts.append(f"\n---\n\n")

    # Summary at top
    parts.append(f"## Summary: {total_passed}/{total_count} passed\n\n")
    for sn, sv in by_set.items():
        parts.append(f"- **{sn}:** {sv['passed']}/{sv['total']}\n")

    parts.append(f"\n| # | Set | Question | Result | Assessment |\n")
    parts.append(f"|---|-----|----------|--------|------------|\n")
    for q_num, sn, q_text, st, smry in summary_rows:
        q_esc = q_text.replace("|", "\\|")
        smry_esc = smry.replace("|", "\\|").replace("\n", " ")
        parts.append(f"| {q_num} | {sn} | {q_esc} | {st} | {smry_esc} |\n")
    parts.append(f"\n---\n\n")

    # Detail body
    parts.extend(body_parts)

    # Write
    log_path.write_text("".join(parts), encoding="utf-8")

    return log_path


def _format_question_log(num, q, r, classification, status, summary):
    """Format one test case result as a markdown string."""
    lines = []
    lines.append(f"## Q{num}: {q['question']}\n\n")
    lines.append(f"**Expected:** {q['expected']}\n\n")
    lines.append(f"**Classification:** {classification}\n\n")
    lines.append(f"**Mode:** {r.get('mode', '?')}\n\n")
    lines.append(f"**Status:** {status}\n\n")
    lines.append(f"**Assessment:** {summary}\n\n")

    if r.get("answer"):
        lines.append(f"**Raw answer:**\n```\n{r['answer'].strip()}\n```\n\n")

    for j, qr in enumerate(r.get("queries", [])):
        tag = "Primary" if qr.get("type") == "primary" else "Supplementary"
        label = qr.get("label", "untitled")
        lines.append(f"### Query {j + 1} [{tag}]: {label}\n\n")
        lines.append(f"```sql\n{qr.get('code', 'N/A')}\n```\n\n")

        if qr.get("result"):
            preview = qr["result"][:2000]
            if len(qr["result"]) > 2000:
                preview += "\n... (truncated)"
            lines.append(f"**Result:**\n```\n{preview}\n```\n\n")

        if qr.get("error"):
            lines.append(f"**Query error:** {qr['error']}\n\n")

    if r.get("narrative"):
        lines.append(f"### Narrative\n\n{r['narrative']}\n\n")

    if r.get("error"):
        lines.append(f"**Error:** {r['error']}\n\n")

    lines.append("---\n\n")
    return "".join(lines)


def _gdrive_link(log_path):
    """Return an HTML string with a link to the log file's parent folder on GDrive.

    Attempts to resolve the Drive folder ID via API. Falls back to a plain
    path display if anything fails (no auth, folder not synced yet, etc.).
    """
    drive_rel = str(log_path).replace("/content/drive/MyDrive/", "")
    fallback = (
        f'<br>📄 <b>Log saved:</b> <code>{log_path.name}</code>'
        f'<br><small>My Drive/{drive_rel}</small>'
    )

    try:
        import logging
        logging.getLogger("google_auth_httplib2").setLevel(logging.ERROR)

        from google.colab import auth
        auth.authenticate_user()

        from googleapiclient.discovery import build
        import google.auth
        creds, _ = google.auth.default()
        service = build("drive", "v3", credentials=creds)

        # Walk folder path to find the logs folder ID
        folder_parts = Path(drive_rel).parent.parts
        parent_id = "root"
        for folder_name in folder_parts:
            resp = service.files().list(
                q=f"name = '{folder_name}' and '{parent_id}' in parents "
                  f"and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
                fields="files(id)",
                pageSize=1,
            ).execute()
            found = resp.get("files", [])
            if not found:
                return fallback
            parent_id = found[0]["id"]

        folder_url = f"https://drive.google.com/drive/folders/{parent_id}"
        return (
            f'<br>📄 <b>{log_path.name}</b> → '
            f'<a href="{folder_url}" target="_blank">Open logs folder</a>'
        )

    except Exception:
        return fallback