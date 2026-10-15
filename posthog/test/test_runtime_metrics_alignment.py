from types import SimpleNamespace

from google.protobuf.json_format import ParseDict
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)

from . import test_host_metrics as host_tests
from . import test_runtime_metrics as runtime_tests
from .test_metrics import sent_payload
from .test_runtime_metrics import attributes

native = runtime_tests.native
runtime_client = runtime_tests.runtime_client
host_native = host_tests.host_native


def metric_groups(session):
    payload = sent_payload(session)
    ParseDict(payload, ExportMetricsServiceRequest())
    return [
        (
            attributes(group["resource"]),
            {
                metric["name"]: metric
                for scope in group["scopeMetrics"]
                for metric in scope["metrics"]
            },
        )
        for group in payload["resourceMetrics"]
    ]


def test_context_switches_use_native_split_not_combined_libproc(
    runtime_client, native, monkeypatch
):
    module, state, _ = native
    counts = SimpleNamespace(voluntary=20, involuntary=300)
    monkeypatch.setattr(
        module,
        "getrusage",
        lambda _: SimpleNamespace(
            ru_utime=state.user,
            ru_stime=state.system,
            ru_nvcsw=counts.voluntary,
            ru_nivcsw=counts.involuntary,
        ),
    )
    client, session = runtime_client()
    counts.voluntary += 4
    counts.involuntary += 9
    state.switches += 99999
    state.size = 8
    client.metrics.flush()
    metrics = metric_groups(session)[0][1]
    metric = metrics["process.context_switches"]
    assert metric["unit"] == "{context_switch}"
    assert metric["sum"]["isMonotonic"] is True
    assert metric["sum"]["aggregationTemporality"] == 2
    assert {
        attributes(point)["process.context_switch.type"]: point["asDouble"]
        for point in metric["sum"]["dataPoints"]
    } == {"voluntary": 4, "involuntary": 9}
    assert "app.process.cpu.ctx_switch.total" not in metrics
    assert "app.process.cpu.percent" not in metrics


def test_runtime_instance_is_resource_scoped_without_changing_manual_or_host(
    runtime_client, host_native
):
    client, session = runtime_client(
        metrics={
            "capture_runtime_metrics": True,
            "capture_host_metrics": True,
            "resource_attributes": {
                "deployment.region": "test",
                "service.instance.id": "configured",
            },
        }
    )
    collector = client.metrics._runtime_metrics
    client.metrics.gauge(
        "process.cpu.utilization",
        42,
        unit="1",
        attributes={
            **collector.attributes,
            "cpu.mode": "user",
        },
    )
    host_native[1].core_ticks[0][0] += 100
    client.metrics.flush()
    groups = metric_groups(session)
    assert len(groups) == 2
    manual_resource, manual = next(
        group for group in groups if group[0]["service.instance.id"] == "configured"
    )
    runtime_resource, runtime = next(
        group for group in groups if "process.cpu.time" in group[1]
    )
    assert (
        runtime_resource["service.instance.id"]
        == collector.resource_attributes["service.instance.id"]
    )
    assert runtime_resource["service.instance.id"] != "configured"
    assert (
        runtime_resource["deployment.region"]
        == manual_resource["deployment.region"]
        == "test"
    )
    assert manual["process.cpu.utilization"]["gauge"]["dataPoints"][0]["asDouble"] == 42
    assert "system.cpu.utilization" in manual
    for metric in runtime.values():
        for point in metric.get("sum", metric.get("gauge", metric.get("histogram")))[
            "dataPoints"
        ]:
            assert (
                attributes(point)["process.pid"] == collector.attributes["process.pid"]
            )
            assert "process.runtime_id" not in attributes(point)
            assert "service.instance.id" not in attributes(point)


def test_failed_export_preserves_resource_identity_when_collector_is_replaced(
    runtime_client, native
):
    client, session = runtime_client()
    first = client.metrics._runtime_metrics
    native[1].user += 1
    session.post.return_value.status_code = 503
    client.metrics.flush()
    client.metrics._start_runtime_metrics()
    second = client.metrics._runtime_metrics
    native[1].user += 2
    session.post.return_value.status_code = 200
    client.metrics.flush()
    groups = metric_groups(session)
    assert {resource["service.instance.id"] for resource, _ in groups} == {
        first.resource_attributes["service.instance.id"],
        second.resource_attributes["service.instance.id"],
    }
    assert sorted(
        next(
            point["asDouble"]
            for point in metrics["process.cpu.time"]["sum"]["dataPoints"]
            if attributes(point)["cpu.mode"] == "user"
        )
        for _, metrics in groups
    ) == [1, 2]


def test_host_memory_states_and_total_without_inventing_unsupported_states(host_native):
    module, state, _ = host_native
    state.active = 200
    state.wired = 30
    state.speculative = 10
    collector = module._MacOSHostMetrics()
    samples = collector.collect()
    memory = [sample for sample in samples if sample.name == "system.memory.usage"]
    assert {
        sample.attributes["system.memory.state"]: sample.value for sample in memory
    } == {
        "free": (state.free - state.speculative) * state.page_size,
        "used": (state.active + state.wired) * state.page_size,
    }
    assert all(
        sample.unit == "By" and sample.metric_type == "observable_updowncounter"
        for sample in memory
    )
    limit = next(sample for sample in samples if sample.name == "system.memory.limit")
    assert limit.value == state.total_memory
    assert limit.unit == "By" and limit.metric_type == "observable_updowncounter"
    names = {sample.name for sample in samples}
    assert "app.system.memory.available.estimated" not in names
    assert "app.system.cpu.utilization" not in names
    assert "system.paging.operations" not in names
    assert "app.system.swap.io" in names
    assert "app.system.memory.pressure" in names
