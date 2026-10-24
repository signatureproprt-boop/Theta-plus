import csv
import io
from datetime import date, datetime, timezone
from types import SimpleNamespace
import pytest
from app.market_collection import parse_master, next_range, advance, five_years_before, target_end
from app.parallel_budget import approved


def master(rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=['EXCH_ID', 'SEGMENT', 'SECURITY_ID', 'INSTRUMENT',
                           'SYMBOL_NAME', 'SM_EXPIRY_DATE', 'ISIN', 'STRIKE_PRICE', 'OPTION_TYPE', 'UNDERLYING_SECURITY_ID'])
    writer.writeheader();writer.writerows(rows)
    return output.getvalue().encode()


def row(**change):
    return {'EXCH_ID': 'NSE', 'SEGMENT': 'E', 'SECURITY_ID': '13', 'INSTRUMENT': 'EQUITY',
            'SYMBOL_NAME': 'EXAMPLE', 'SM_EXPIRY_DATE': '', 'ISIN': 'EXAMPLE_ISIN', **change}


def progress(**change):
    return SimpleNamespace(interval='1', floor=date(2021, 10, 24), older_to=date(2026, 10, 24),
        recent_end=None, pending_job_id='kept', state='WAITING', **change)


def test_commodity_rows_are_counted_on_hold_without_mapping():
    rows, stats = parse_master(master([row(), row(EXCH_ID='MCX', SEGMENT='M', INSTRUMENT='FUTCOM'),
                                         row(SEGMENT='M', INSTRUMENT='OPTFUT')]))
    assert len(rows) == 1 and stats['commodity_rows_on_hold'] == 2
    assert rows[0]['exchange_segment'] == 'NSE_EQ'


def test_same_security_id_on_different_exchanges_is_not_merged():
    rows, _ = parse_master(master([row(), row(EXCH_ID='BSE')]))
    assert {r['exchange_segment'] for r in rows} == {'NSE_EQ', 'BSE_EQ'}


def test_conflicting_identities_in_one_master_abort_instead_of_selecting_one():
    one = row(SEGMENT='D', INSTRUMENT='FUTSTK', SM_EXPIRY_DATE='2026-10-27')
    with pytest.raises(ValueError, match='AMBIGUOUS'):
        parse_master(master([one, {**one, 'SM_EXPIRY_DATE': '2026-11-24'}]))


def test_missing_or_invalid_expiry_is_review_not_automatic_contract_guess():
    rows, summary = parse_master(master([row(), row(SEGMENT='D', INSTRUMENT='FUTSTK')]))
    assert len(rows) == 1 and summary['other_mapping_review_rows'] == 1


def test_schema_error_and_empty_supported_response_are_not_imported():
    for content in (b'wrong,columns\nx,y\n', master([row(EXCH_ID='MCX', SEGMENT='M')])):
        with pytest.raises(ValueError):
            parse_master(content)


def test_recent_probe_uses_last_session_across_weekend_and_verified_holiday():
    p = progress()
    assert next_range(p, date(2026, 10, 25)) == (date(2026, 10, 23), date(2026, 10, 25))
    assert next_range(p, date(2026, 10, 21)) == (date(2026, 10, 19), date(2026, 10, 21))


def test_range_is_capped_at_contract_expiry_and_calendar_floor():
    p = progress()
    start, end = next_range(p, date(2026, 10, 24), date(2026, 10, 22))
    assert end == date(2026, 10, 23) and start >= p.floor


def test_rows_advance_checkpoint_but_failure_keeps_original_range_for_review():
    p = progress()
    job = SimpleNamespace(status='ROWS_RECEIVED', start_date=date(2026, 10, 23), end_date=date(2026, 10, 24))
    advance(p, job)
    assert p.older_to == date(2026, 10, 23) and p.recent_end == job.end_date
    assert p.pending_job_id is None and p.state == 'READY'
    p.pending_job_id = 'original';job.status = 'FAILED'
    advance(p, job)
    assert p.state == 'REVIEW_REQUIRED' and p.pending_job_id == 'original'
    assert p.older_to == date(2026, 10, 23)


def test_older_windows_are_adjacent_and_daily_catchup_is_prioritized():
    p = progress();p.recent_end = date(2026, 10, 24);p.older_to = date(2026, 10, 23)
    assert next_range(p, date(2026, 10, 24)) == (date(2026, 10, 16), date(2026, 10, 23))
    assert next_range(p, date(2026, 10, 27)) == (date(2026, 10, 24), date(2026, 10, 27))


def test_parallel_budget_requires_compatible_activated_legacy_release():
    valid = {'coordination': 'parallel-collectors-v1', 'legacy_daily_guard': 50000,
             'legacy_global_spacing_seconds': .5, 'worker_release_matches': True}
    assert approved(valid)
    for change in [{'legacy_daily_guard': 100000}, {'worker_release_matches': False}, {'legacy_global_spacing_seconds': .25}]:
        assert not approved({**valid, **change})


def test_leap_horizon_and_late_daily_publication_boundary():
    assert five_years_before(date(2024, 2, 29)) == date(2019, 2, 28)
    assert target_end(datetime(2026, 10, 24, 19, 0, tzinfo=timezone.utc)) == date(2026, 10, 24)
