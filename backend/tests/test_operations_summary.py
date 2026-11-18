from backend.services.operations_summary import summarize_runs


def test_summary_is_user_scoped_and_reports_unknown_metrics():
    summary = summarize_runs([{'user_id': 'bob', 'run_id': 'private', 'status': 'FAILED'}], 'alice')
    assert summary['sample_size'] == 0
    assert summary['completion_rate'] is None
    assert summary['median_completion_seconds'] is None
    assert summary['attention'] == []
    # Cost figures are token estimates (audit: "cost_tracking not_available");
    # with no visible runs the estimate is zero, mode still reported.
    assert summary['cost_tracking']['mode'] == 'estimated'
    assert summary['cost_tracking']['estimated_tokens'] == 0
    assert summary['cost_tracking']['estimated_cost_usd'] == 0.0


def test_completion_rate_durations_failures_and_approval_attention():
    summary = summarize_runs([
        {'user_id': 'alice', 'run_id': 'done', 'status': 'COMPLETED',
         'created_at': '2026-01-01T00:00:00Z', 'tasks': [{'completed_at': '2026-01-01T00:00:20Z'}]},
        {'user_id': 'alice', 'run_id': 'failed', 'status': 'FAILED',
         'tasks': [{'agent': 'core.http', 'status': 'FAILED'}]},
        {'user_id': 'alice', 'run_id': 'review', 'status': 'RUNNING',
         'tasks': [{'status': 'WAITING_APPROVAL'}]},
    ], 'alice')
    assert summary['completion_rate'] == 50
    assert summary['median_completion_seconds'] == 20
    assert summary['failed_tasks_by_agent'] == {'core.http': 1}
    assert {r['reason'] for r in summary['attention']} == {'failed', 'approval_required'}
