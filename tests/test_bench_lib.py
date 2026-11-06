import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from bench_lib import RunResult, parse_stream, percentile, summarize  # noqa: E402

DONE = ('{"message":{"content":""},"done":true,"done_reason":"stop","prompt_eval_count":40,'
        '"prompt_eval_duration":2000000000,"eval_count":10,"eval_duration":500000000,'
        '"load_duration":1000000000}')


def test_parse_stream_measures_ttft_and_rates():
    lines = [(10.5, '{"message":{"content":""},"done":false}'),
             (10.8, '{"message":{"content":"4"},"done":false}'),
             (11.0, DONE)]
    r = parse_stream(lines, started=10.0)
    assert r.ttft_s == pytest.approx(0.8) and r.wall_s == pytest.approx(1.0)
    assert r.prompt_tok_per_s == pytest.approx(20.0) and r.output_tok_per_s == pytest.approx(20.0)
    assert r.load_s == pytest.approx(1.0) and r.done_reason == "stop" and r.text == "4"


def test_parse_stream_without_done_chunk_is_rejected():
    with pytest.raises(ValueError):
        parse_stream([(1.0, '{"message":{"content":"x"},"done":false}')], started=0.0)


def test_percentile_nearest_rank():
    assert percentile([5, 1, 3, 2, 4], 50) == 3
    assert percentile(list(range(1, 21)), 95) == 19
    assert percentile([7], 95) == 7
    with pytest.raises(ValueError):
        percentile([], 50)


def test_summarize_skips_missing_values():
    runs = [RunResult(None, 1.0, 0.0, 1, 1, None, 10.0, "stop", ""),
            RunResult(0.5, 2.0, 0.0, 1, 1, 5.0, 20.0, "stop", "")]
    s = summarize(runs)
    assert s["n"] == 2 and s["ttft_p50"] == 0.5 and s["wall_p95"] == 2.0
    assert s["output_tok_per_s_p50"] == 10.0
