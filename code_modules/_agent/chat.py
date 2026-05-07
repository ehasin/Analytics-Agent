"""
chat.py — Thin CLI wrapper around the analytics agent.

Handles CLI I/O only: input(), print(), markdown display, logger calls, and the
REPL loop itself. All conversation orchestration (interpret, mode inference,
summary update, trace enrichment) lives in conversation_processor.py so the
Streamlit frontend can reuse it.

Provides:
  - create_chat_fn   Factory that returns a zero-arg chat() function
                     with all dependencies baked in.
"""

import time

from IPython.display import display, Markdown, HTML

from _agent.analyst_agent import (
    MODE_NAMES, extract_user_context,
)
from _agent.conversation_processor import (
    parse_mode_command, CMD_TO_MODE, MODE_TRANSITION_MSGS,
    interpret_turn, update_summary, enrich_trace,
)
from _skills.session_logger import SessionLogger


# ── Factory ──────────────────────────────────────────────────

def create_chat_fn(
    agent_fn, llm_fn, logs_dir, backend: str, model_name: str, log_prefix: str = ""
):
    """Return a zero-arg chat() function with all dependencies baked in.

    Args:
        agent_fn:    callable(question, mode, user_context) → result dict.
                     Defined once; the agent itself routes Reason-mode narration
                     to the stronger model via llm_fn(prompt, tier=2) internally.
        llm_fn:      callable(prompt, tier=1) → str. Used here for interpret
                     (tier 0) and summary update (tier 0).
        logs_dir:    Path to Logs directory
        backend:     backend name (for display / log header)
        model_name:  tier-1 model string (for log header)
        log_prefix:  optional filename prefix, e.g. "Project_1.0_branch_"
                     prepended to "chat_log_YYYY_MM_DD_HH_MM.md"
    """

    def chat():
        history = []
        summary = ""            # rolling session summary (updated end-of-turn)
        current_mode = 0
        mode_control = "auto"
        user_context = None
        turn_num = 0

        logger = SessionLogger(
            logs_dir=logs_dir, backend=backend, model_name=model_name,
            prefix=f"{log_prefix}chat_log" if log_prefix else "chat_log",
        )

        print(f"Analytics Agent [{backend}]  •  {MODE_NAMES[current_mode]} mode (auto)")
        print("Commands: /retrieve /explore /reason (lock mode) · /auto (unlock) · quit")
        print("=" * 60)

        while True:
            try:
                question = input("\nYou: ")
            except (KeyboardInterrupt, EOFError):
                print("\nSession ended.")
                break

            if question.strip().lower() in ("quit", "exit", "q"):
                print("Session ended.")
                break

            # Per-turn wall clock + trace accumulator
            turn_start = time.time()
            turn_trace: list[dict] = []

            # ── Stage 0: parse slash command ──
            # Update mode_control / current_mode silently. Mode line is printed
            # later by the resolution block (after Interpret), so order is:
            #   Interpreting → Resolved → Interaction type → Mode → Planning.
            # Exception: command-only input (no question) announces immediately
            # and loops.
            question, cmd = parse_mode_command(question)
            if cmd is not None:
                if cmd == "auto":
                    mode_control = "auto"
                else:
                    current_mode = CMD_TO_MODE[cmd]
                    mode_control = "manual"

                if not question.strip():
                    # Command-only input: announce and loop
                    if cmd == "auto":
                        print(f"  → Mode: returned to AUTO (current: {MODE_NAMES[current_mode]})")
                    else:
                        print(
                            f"  → Mode: {MODE_NAMES[current_mode]}   "
                            f"→ Locked to {MODE_NAMES[current_mode]} (by user). Type /auto to unlock."
                        )
                    continue

            turn_num += 1

            # Defaults in case we fail before these are populated
            resolved = question
            turn_type = None
            active_mode = current_mode
            r: dict = {}
            turn_status = "complete"
            turn_error: str | None = None

            try:
                # Detect explicit user role once
                if not user_context:
                    detected = extract_user_context(question)
                    if detected:
                        user_context = detected
                        print(f"  Context noted: {user_context}")

                # ── Stage 1: interpret turn ──
                last_turn_verbatim = ""
                if history:
                    h = history[-1]
                    last_turn_verbatim = f"User: {h['question']}\nBot: {h['narrative']}"

                print("  → Interpreting...")
                resolved, suggested_mode, mode_reason, turn_type, interp_record = interpret_turn(
                    question=question,
                    summary=summary,
                    last_turn=last_turn_verbatim,
                    turn_count=turn_num,
                    current_mode=current_mode,
                    mode_control=mode_control,
                    llm_fn=llm_fn,
                )
                turn_trace.append(interp_record)
                if resolved != question:
                    print(f"  → Resolved as: {resolved}")
                if turn_type:
                    print(f"  → Interaction type: {turn_type}")

                # ── Stage 1b: resolve mode ──
                if mode_control == "manual":
                    active_mode = current_mode
                    mode_line = (
                        f"  → Mode: {MODE_NAMES[active_mode]}   "
                        f"→ Locked to {MODE_NAMES[active_mode]} (by user). Type /auto to unlock."
                    )
                else:
                    active_mode = suggested_mode if suggested_mode is not None else current_mode
                    if active_mode != current_mode:
                        # Mode changed in auto — combine transition explainer with mode
                        mode_line = f"  → Mode: {MODE_TRANSITION_MSGS[active_mode]}"
                    else:
                        # Unchanged or first-set — just state the mode
                        mode_line = f"  → Mode: {MODE_NAMES[active_mode]}"

                current_mode = active_mode
                print(mode_line)

                # ── Stage 2+3: run agent ──
                print("  → Planning and executing queries...")
                r = agent_fn(resolved, mode=active_mode, user_context=user_context)
                turn_trace.extend(r.get("stage_trace", []))

                # Display (escape $ to avoid MathJax rendering in notebook)
                safe_reply = r["narrative"].replace("$", r"\$")
                display(Markdown(f"**Bot:** {safe_reply}"))

                # ── Stage 5: update rolling summary ──
                print("  → Updating summary...")
                new_summary, summ_record = update_summary(
                    summary, question, r["narrative"], llm_fn,
                )
                summary = new_summary
                if summ_record is not None:
                    turn_trace.append(summ_record)

            except KeyboardInterrupt:
                turn_status = "interrupted"
                turn_error = "Interrupted by user (Ctrl+C)"
                print(f"\n  ⚠ Turn interrupted. Partial trace will be logged.")
            except Exception as e:
                turn_status = "failed"
                turn_error = f"{type(e).__name__}: {e}"
                print(f"\n  ⚠ Turn failed: {turn_error}")
            finally:
                # Always enrich, display timing, log, and update history — even on failure
                enriched_trace = enrich_trace(turn_trace, backend)
                total_seconds = round(time.time() - turn_start, 2)

                # Timing banner
                status_tag = "" if turn_status == "complete" else f"  ({turn_status})"
                print(f"⏱  {total_seconds}s total{status_tag}")

                # SQL fold (only if we got that far)
                if r.get("code"):
                    display(HTML(
                        f"<details><summary>🔍 Show SQL</summary>"
                        f"<pre>{r['code']}</pre></details>"
                    ))

                # Log — always, even on failure/interrupt
                try:
                    logger.log_turn(
                        turn_num=turn_num, question=question, resolved=resolved,
                        mode=active_mode, mode_name=MODE_NAMES[active_mode],
                        classification=r.get("classification", "unknown"),
                        queries=r.get("queries", []),
                        narrative=r.get("narrative", ""),
                        error=turn_error or r.get("error"),
                        user_context=user_context,
                        turn_type=turn_type,
                        stage_trace=enriched_trace,
                        total_seconds=total_seconds,
                        turn_status=turn_status,
                    )
                except Exception as log_err:
                    print(f"  ⚠ Logging failed: {log_err}")

                # History — only add successful turns (interrupted/failed turns shouldn't poison follow-ups)
                if turn_status == "complete":
                    history.append({
                        "question": question,
                        "resolved": resolved,
                        "narrative": r.get("narrative", ""),
                        "code": r.get("code", ""),
                        "mode": active_mode,
                        "classification": r.get("classification", "can_answer"),
                    })

    return chat
