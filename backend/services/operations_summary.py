"""Observed workflow metrics, bounded to the caller's recent run sample.

Token/cost figures are *estimates* (≈4 chars/token over goal + task
payloads) because the model pipeline does not return usage metadata.
Per-model USD rates come from settings (ESTIMATED_COST_PER_1M_TOKENS)
with a conservative default; they are planning numbers, not billing.
"""
from collections import Counter
from datetime import datetime, timezone
from statistics import median

CHARS_PER_TOKEN = 4
DEFAULT_COST_PER_1M_TOKENS = 0.60  # USD per 1M estimated tokens (blended)


def _estimate_tokens(run):
    def tok(text):
        return max(0, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN) if text else 0
    import json
    total = tok(str(run.get("goal") or ""))
    for task in run.get("tasks") or []:
        total += tok(json.dumps(task.get("output_data") or {}, default=str)[:8000])
        total += tok(str(task.get("error") or ""))
        total += tok(json.dumps(task.get("input_data") or {}, default=str)[:4000])
    return total


def _cost_rate(model: str) -> float:
    try:
        from backend.config.settings import settings
        overrides = {}
        raw = getattr(settings, "MODEL_COST_PER_1M_TOKENS", "")
        for pair in str(raw or "").split(","):
            if ":" in pair:
                name, rate = pair.split(":", 1)
                try:
                    overrides[name.strip().lower()] = float(rate)
                except ValueError:
                    continue
        return overrides.get((model or "").lower(), DEFAULT_COST_PER_1M_TOKENS)
    except Exception:
        return DEFAULT_COST_PER_1M_TOKENS


def parse_time(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except (TypeError, ValueError):
        return None


def summarize_runs(runs, user_id):
    runs = [r for r in runs if r.get('user_id') == user_id]
    counts = Counter(r.get('status', 'UNKNOWN') for r in runs)
    durations, failures, attention = [], Counter(), []
    now = datetime.now(timezone.utc)
    for run in runs:
        started = parse_time(run.get('created_at'))
        completed_times = []
        tasks = run.get('tasks') or []
        for task in tasks:
            if task.get('status') == 'FAILED':
                failures[task.get('agent') or 'unknown'] += 1
            ended = parse_time(task.get('completed_at'))
            if ended:
                completed_times.append(ended)
        if run.get('status') == 'COMPLETED' and started and completed_times:
            durations.append(max(0, (max(completed_times) - started).total_seconds()))
        waiting = any(t.get('status') == 'WAITING_APPROVAL' for t in tasks)
        age = max(0, (now - started).total_seconds()) if started else None
        if waiting or run.get('status') == 'FAILED' or (
            run.get('status') in ('RUNNING', 'PENDING', 'WAITING') and age is not None and age > 900
        ):
            attention.append({'run_id': run['run_id'], 'status': run.get('status'),
                              'reason': 'approval_required' if waiting else 'failed' if run.get('status') == 'FAILED' else 'long_running',
                              'age_seconds': age})
    completed = counts['COMPLETED']
    failed = counts['FAILED']
    estimated_tokens = sum(_estimate_tokens(r) for r in runs)
    cost_rate = _cost_rate(None)
    try:
        from backend.config.settings import settings
        cost_rate = _cost_rate(getattr(settings, "GEMINI_MODEL", ""))
    except Exception:
        pass
    estimated_cost_usd = round(estimated_tokens / 1_000_000 * cost_rate, 4)
    return {
        'sample_size': len(runs), 'sample_limit': 200, 'status_counts': dict(counts),
        'completion_rate': round(completed / (completed + failed) * 100, 1) if completed + failed else None,
        'median_completion_seconds': round(median(durations), 1) if durations else None,
        'failed_tasks_by_agent': dict(failures), 'attention': attention,
        'generated_at': now.isoformat(),
        'cost_tracking': {
            'mode': 'estimated',
            'note': 'Token counts are ~4 chars/token estimates over goals and task payloads; '
                    'rates are planning numbers, not billing data.',
            'estimated_tokens': estimated_tokens,
            'estimated_cost_usd': estimated_cost_usd,
            'usd_per_1m_tokens': cost_rate,
        },
    }
