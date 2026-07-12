from __future__ import annotations

import logging

from sn2md_worker.logging import HealthProbeAccessFilter, configure_logging

# Uvicorn 0.50.0 (`protocols/http/h11_impl.py`) logs every access line as
# `access_logger.info('%s - "%s %s HTTP/%s" %d', client_addr, method,
# path_with_query_string, http_version, status_code)`. The helpers below
# build LogRecords with that exact msg/args shape so these tests pin the
# real contract the filter parses.
_ACCESS_LOG_MSG = '%s - "%s %s HTTP/%s" %d'


def _make_access_record(
    *,
    method: str = "GET",
    path: str = "/healthz",
    status_code: int = 200,
    client_addr: str = "127.0.0.1:43420",
    http_version: str = "1.1",
) -> logging.LogRecord:
    return _make_record_with_args((client_addr, method, path, http_version, status_code))


def _make_record_with_args(args: tuple[object, ...] | None) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg=_ACCESS_LOG_MSG,
        args=args,
        exc_info=None,
    )


class TestWhenAHealthProbeSucceeds:
    def test_healthz_record_is_dropped(self) -> None:
        # GIVEN
        record = _make_access_record(path="/healthz", status_code=200)

        # WHEN
        kept = HealthProbeAccessFilter().filter(record)

        # THEN
        assert kept is False

    def test_readyz_record_is_dropped(self) -> None:
        # GIVEN
        record = _make_access_record(path="/readyz", status_code=200)

        # WHEN
        kept = HealthProbeAccessFilter().filter(record)

        # THEN
        assert kept is False

    def test_healthz_with_query_string_is_dropped(self) -> None:
        # GIVEN: uvicorn logs the path with its query string appended
        record = _make_access_record(path="/healthz?x=1", status_code=200)

        # WHEN
        kept = HealthProbeAccessFilter().filter(record)

        # THEN
        assert kept is False

    def test_redirect_status_counts_as_success_and_is_dropped(self) -> None:
        # GIVEN: anything below 400 is a non-failure probe response
        record = _make_access_record(path="/readyz", status_code=307)

        # WHEN
        kept = HealthProbeAccessFilter().filter(record)

        # THEN
        assert kept is False


class TestWhenAHealthProbeFails:
    def test_5xx_readyz_record_is_kept(self) -> None:
        # GIVEN: the not-ready 503 the readiness probe returns
        record = _make_access_record(path="/readyz", status_code=503)

        # WHEN
        kept = HealthProbeAccessFilter().filter(record)

        # THEN
        assert kept is True

    def test_4xx_healthz_record_is_kept(self) -> None:
        # GIVEN: 400 is the first failure status; pin the boundary
        record = _make_access_record(path="/healthz", status_code=400)

        # WHEN
        kept = HealthProbeAccessFilter().filter(record)

        # THEN
        assert kept is True


class TestWhenOtherTrafficIsLogged:
    def test_status_endpoint_record_is_kept(self) -> None:
        # GIVEN: /status is a human endpoint, never a probe
        record = _make_access_record(path="/status", status_code=200)

        # WHEN
        kept = HealthProbeAccessFilter().filter(record)

        # THEN
        assert kept is True

    def test_non_get_request_to_a_probe_path_is_kept(self) -> None:
        # GIVEN
        record = _make_access_record(method="POST", path="/healthz", status_code=200)

        # WHEN
        kept = HealthProbeAccessFilter().filter(record)

        # THEN
        assert kept is True

    def test_probe_path_prefix_is_not_treated_as_a_probe(self) -> None:
        # GIVEN: only the exact paths /healthz and /readyz are probes
        record = _make_access_record(path="/healthz/extra", status_code=200)

        # WHEN
        kept = HealthProbeAccessFilter().filter(record)

        # THEN
        assert kept is True


class TestWhenAnAccessRecordHasAnUnexpectedShape:
    def test_missing_args_record_is_kept(self) -> None:
        # GIVEN: a record with no args at all
        record = _make_record_with_args(None)

        # WHEN
        kept = HealthProbeAccessFilter().filter(record)

        # THEN: fail open: when in doubt, log the line
        assert kept is True

    def test_wrong_arity_args_record_is_kept(self) -> None:
        # GIVEN: a four-tuple instead of uvicorn's five
        record = _make_record_with_args(("127.0.0.1:43420", "GET", "/healthz", "1.1"))

        # WHEN
        kept = HealthProbeAccessFilter().filter(record)

        # THEN
        assert kept is True

    def test_non_integer_status_record_is_kept(self) -> None:
        # GIVEN: a status rendered as a string rather than an int
        record = _make_record_with_args(("127.0.0.1:43420", "GET", "/healthz", "1.1", "200"))

        # WHEN
        kept = HealthProbeAccessFilter().filter(record)

        # THEN
        assert kept is True

    def test_non_string_path_record_is_kept(self) -> None:
        # GIVEN: a path that is not a str
        record = _make_record_with_args(("127.0.0.1:43420", "GET", None, "1.1", 200))

        # WHEN
        kept = HealthProbeAccessFilter().filter(record)

        # THEN
        assert kept is True


class TestWhenLoggingIsConfigured:
    def test_uvicorn_access_logger_gets_the_probe_filter_exactly_once(self) -> None:
        # GIVEN: pristine uvicorn.access logger; snapshot global state so
        # configure_logging's basicConfig(force=True) is undone afterwards
        access_logger = logging.getLogger("uvicorn.access")
        original_filters = list(access_logger.filters)
        access_logger.filters = [
            f for f in access_logger.filters if not isinstance(f, HealthProbeAccessFilter)
        ]
        root_logger = logging.getLogger()
        original_handlers = list(root_logger.handlers)
        original_level = root_logger.level

        try:
            # WHEN: configured twice, as repeated boots in one process would
            configure_logging("INFO")
            configure_logging("INFO")

            # THEN: the filter is installed on uvicorn.access, without duplicates
            installed = [f for f in access_logger.filters if isinstance(f, HealthProbeAccessFilter)]
            assert len(installed) == 1
        finally:
            access_logger.filters = original_filters
            root_logger.handlers = original_handlers
            root_logger.setLevel(original_level)
