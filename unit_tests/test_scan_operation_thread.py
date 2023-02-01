"""
Unit tests for scan_operation_thread.py
contains 3 tests that check scan operations behavior in different conditions

test_scan_positive - positive scenario
test_scan_negative_operation_timed_out - getting operation_timed_out in scan execution
test_scan_negative_exception - getting operation_timed_out in scan execution (with and without nemesis)
"""
from pathlib import Path
import os
from threading import Event
from unittest.mock import MagicMock, patch
import pytest
from cassandra import OperationTimedOut
from unit_tests.test_cluster import DummyDbCluster, DummyNode
from unit_tests.lib.events_utils import EventsUtilsMixin
from sdcm.scan_operation_thread import ScanOperationThread, FullScanParams


DEFAULT_PARAMS = {
    'termination_event': Event(),
    'fullscan_user': 'sla_role_name',
    'fullscan_user_password': 'sla_role_password',
    'duration': 10,
    'interval': 0,
    'validate_data': True
}


class DBCluster(DummyDbCluster):  # pylint: disable=abstract-method
    # pylint: disable=super-init-not-called
    def __init__(self, connection_mock, nodes, params):
        super().__init__(nodes, params=params)
        self.connection_mock = connection_mock
        self.params = {'nemesis_seed': 1}

    def get_non_system_ks_cf_list(*args, **kwargs):
        # pylint: disable=unused-argument
        # pylint: disable=no-method-argument
        return ["test", "a.b"]

    def cql_connection_patient(self, *args, **kwargs):
        # pylint: disable=unused-argument
        return self.connection_mock


def get_event_log_file(module_events):  # pylint: disable=redefined-outer-name
    if (log_file := Path(module_events.temp_dir, "events_log", "events.log")).exists():
        return log_file.read_text(encoding="utf-8").rstrip().split('\n')
    return ""


@pytest.fixture(scope='module')
def module_events():
    mixing = EventsUtilsMixin()
    mixing.setup_events_processes(events_device=True, events_main_device=False, registry_patcher=True)
    yield mixing
    mixing.teardown_events_processes()


@pytest.fixture(scope='function', autouse=True)
def cleanup_event_log_file(module_events):  # pylint: disable=redefined-outer-name
    with open(os.path.join(module_events.temp_dir, "events_log", "events.log"), 'r+', encoding="utf-8") as file:
        file.truncate(0)


@pytest.fixture(scope='module', autouse=True)
def mock_get_partition_keys():
    with patch('sdcm.scan_operation_thread.get_partition_keys'):
        yield


@pytest.fixture(scope='module')
def node():
    return DummyNode(name='test_node',
                     parent_cluster=None,
                     ssh_login_info=dict(key_file='~/.ssh/scylla-test'))


class MockCqlConnectionPatient(MagicMock):
    def execute_async(*args, **kwargs):
        # pylint: disable=unused-argument
        # pylint: disable=no-method-argument
        class MockFuture:
            # pylint: disable=too-few-public-methods
            has_more_pages = False

            def add_callbacks(self, callback, errback):
                # pylint: disable=unused-argument
                # pylint: disable=no-self-use
                callback([MagicMock()])
        return MockFuture()

    events = ["Dispatching forward_request to 1 endpoints"]


@pytest.fixture(scope='module')
def cluster(node):  # pylint: disable=redefined-outer-name
    db_cluster = DBCluster(MockCqlConnectionPatient(), [node], {})
    node.parent_cluster = db_cluster
    return db_cluster


@pytest.mark.parametrize("mode", ['table', 'partition', 'aggregate'])
def test_scan_positive(mode, module_events, cluster):  # pylint: disable=redefined-outer-name
    default_params = FullScanParams(
        db_cluster=cluster,
        ks_cf='a.b',
        mode=mode,
        **DEFAULT_PARAMS
    )
    with module_events.wait_for_n_events(module_events.get_events_logger(), count=2, timeout=10):
        ScanOperationThread(default_params)._run_next_scan_operation()  # pylint: disable=protected-access
    all_events = get_event_log_file(module_events)
    assert "Severity.NORMAL" in all_events[0] and "period_type=begin" in all_events[0]
    assert "Severity.NORMAL" in all_events[1] and "period_type=end" in all_events[1]
    if mode == "aggregate":
        assert "MockCqlConnectionPatient" in all_events[1]


########################################################################################################################
class ExecuteOperationTimedOutMockCqlConnectionPatient(MockCqlConnectionPatient):
    def execute(*args, **kwargs):
        # pylint: disable=unused-argument
        # pylint: disable=no-method-argument
        raise OperationTimedOut("timeout")


class ExecuteAsyncOperationTimedOutMockCqlConnectionPatient(MockCqlConnectionPatient):
    def execute_async(*args, **kwargs):
        # pylint: disable=unused-argument
        # pylint: disable=no-method-argument
        raise OperationTimedOut("timeout")


@pytest.mark.parametrize(("mode", 'severity', 'timeout', 'execute_mock'),
                         [['partition', 'WARNING', 0, 'execute_async'],
                          ['aggregate', 'WARNING', 60*30, 'execute'],
                          ['aggregate', 'ERROR', 0, 'execute'],
                          ['table', 'WARNING', 0, 'execute']])
def test_scan_negative_operation_timed_out(mode, severity, timeout, execute_mock, module_events, node):
    # pylint: disable=redefined-outer-name
    # pylint: disable=too-many-arguments
    if execute_mock == 'execute_async':
        connection = ExecuteAsyncOperationTimedOutMockCqlConnectionPatient()
    else:
        connection = ExecuteOperationTimedOutMockCqlConnectionPatient()
    db_cluster = DBCluster(connection, [node], {})
    node.parent_cluster = db_cluster
    default_params = FullScanParams(
        db_cluster=db_cluster,
        ks_cf='a.b',
        mode=mode,
        aggregate_operation_limit=timeout,
        **DEFAULT_PARAMS
    )
    with module_events.wait_for_n_events(module_events.get_events_logger(), count=2, timeout=10):
        ScanOperationThread(default_params)._run_next_scan_operation()  # pylint: disable=protected-access
    all_events = get_event_log_file(module_events)
    assert "Severity.NORMAL" in all_events[0] and "period_type=begin" in all_events[0]
    assert f"Severity.{severity}" in all_events[1] and "period_type=end" in all_events[1]


########################################################################################################################
class ExecuteExceptionMockCqlConnectionPatient(MockCqlConnectionPatient):
    def execute(*args, **kwargs):
        # pylint: disable=unused-argument
        # pylint: disable=no-method-argument
        raise Exception("Exception")


class ExecuteAsyncExceptionMockCqlConnectionPatient(MockCqlConnectionPatient):
    def execute_async(*args, **kwargs):
        # pylint: disable=unused-argument
        # pylint: disable=no-method-argument
        raise Exception("Exception")


@pytest.mark.parametrize(("running_nemesis", 'severity'), [[True, 'WARNING'], [False, 'ERROR']])
@pytest.mark.parametrize(('mode', 'execute_mock'), [
    ['partition', 'execute_async'],
    ['aggregate', 'execute'],
    ['table', 'execute']])
def test_scan_negative_exception(mode, severity, running_nemesis, execute_mock, module_events, node):
    # pylint: disable=redefined-outer-name
    # pylint: disable=too-many-arguments
    if running_nemesis:
        node.running_nemesis = MagicMock()
    else:
        node.running_nemesis = None
    if execute_mock == 'execute_async':
        connection = ExecuteAsyncExceptionMockCqlConnectionPatient()
    else:
        connection = ExecuteExceptionMockCqlConnectionPatient()
    db_cluster = DBCluster(connection, [node], {})
    node.parent_cluster = db_cluster
    default_params = FullScanParams(
        db_cluster=db_cluster,
        ks_cf='a.b',
        mode=mode,
        ** DEFAULT_PARAMS
    )
    with module_events.wait_for_n_events(module_events.get_events_logger(), count=2, timeout=10):
        ScanOperationThread(default_params)._run_next_scan_operation()  # pylint: disable=protected-access
    all_events = get_event_log_file(module_events)
    assert "Severity.NORMAL" in all_events[0] and "period_type=begin" in all_events[0]
    assert f"Severity.{severity}" in all_events[1] and "period_type=end" in all_events[1]
