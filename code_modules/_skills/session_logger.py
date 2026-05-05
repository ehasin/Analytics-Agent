"""
session_logger.py — Markdown session logger for chat and eval runs.

Creates timestamped log files in the project Logs directory.
Captures: user question, resolved question, mode, classification,
execution plan, SQL queries, results, narrative, and errors.

Provides:
  - SessionLogger   Class managing a single log file
"""

import datetime
from pathlib import Path


class SessionLogger:
    """Append-only markdown logger for one chat or eval session."""

    def __init__(
        self,
        logs_dir: str | Path,
        backend: str,
        model_name: str,
        prefix: str = "chat_log",
        timestamp: datetime.datetime | None = None,
    ):
        logs_dir = Path(logs_dir)
        logs_dir.mkdir(parents=True, exist_ok=True)

        self.timestamp = timestamp or datetime.datetime.now()
        ts_str = self.timestamp.strftime("%Y_%m_%d_%H_%M")
        self.path = logs_dir / f"{prefix}_{ts_str}.md"
        self.backend = backend
        self.model_name = model_name

        with self.path.open("w", encoding="utf-8") as f:
            f.write(f"# Session Log\n\n")
            f.write(f"**Started:** {self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"**Backend:** {backend}\n")
            f.write(f"**Model:** {model_name}\n\n---\n\n")

        print(f"Session log: {self.path}")

    def log_turn(
        self,
        turn_num: int,
        question: str,
        resolved: str,
        mode: int,
        mode_name: str,
        classification: str,
        queries: list[dict],
        narrative: str,
        error: str | None = None,
        user_context: str | None = None,
        turn_type: str | None = None,
        stage_trace: list[dict] | None = None,
        total_seconds: float | None = None,
        turn_status: str = "complete",
    ):
        """Append one Q&A turn to the log file."""
        import json

        with self.path.open("a", encoding="utf-8") as f:
            f.write(f"## Turn {turn_num}\n\n")

            # Status banner for non-complete turns
            if turn_status != "complete":
                f.write(f"> **⚠ Turn {turn_status.upper()}** — partial data below\n\n")

            # User question
            f.write(f"**User:** {question}\n\n")
            if resolved != question:
                f.write(f"**Resolved:** {resolved}\n\n")

            # Context, turn type, classification
            if user_context:
                f.write(f"**User context:** {user_context}\n\n")
            header_bits = [f"**Mode:** {mode_name} ({mode})"]
            if turn_type:
                header_bits.append(f"**Turn type:** {turn_type}")
            header_bits.append(f"**Classification:** {classification}")
            f.write(" | ".join(header_bits) + "\n\n")

            # Timing + model trace
            if stage_trace:
                flat = " → ".join(
                    f"{r.get('stage','?')}({r.get('model','?')},{r.get('seconds','?')}s)"
                    for r in stage_trace
                )
                total_str = f" | total {total_seconds}s" if total_seconds is not None else ""
                f.write(f"**Trace:** {flat}{total_str}\n\n")
                f.write("<details><summary>Stage trace detail</summary>\n\n")
                f.write("```json\n")
                f.write(json.dumps(stage_trace, indent=2))
                f.write("\n```\n\n</details>\n\n")

            # Error (if any)
            if error:
                f.write(f"**Error:** {error}\n\n")

            # Queries: SQL + results
            for i, q in enumerate(queries):
                tag = q.get("type", "unknown").upper()
                label = q.get("label", "untitled")
                f.write(f"### Query {i + 1} [{tag}]: {label}\n\n")
                f.write(f"```sql\n{q.get('code', 'N/A')}\n```\n\n")

                if q.get("result"):
                    preview = q["result"][:2000]
                    if len(q["result"]) > 2000:
                        preview += "\n... (truncated)"
                    f.write(f"**Result:**\n```\n{preview}\n```\n\n")

                if q.get("error"):
                    f.write(f"**Query error:** {q['error']}\n\n")

            # Narrative
            f.write(f"### Narrative\n\n{narrative}\n\n")
            f.write(f"---\n\n")
            
            
    def log_raw(self, text: str):
        """Append arbitrary text (for eval summaries, notes, etc.)."""
        with self.path.open("a", encoding="utf-8") as f:
            f.write(text)