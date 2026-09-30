import time

import psutil
from prometheus_client import CollectorRegistry, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

EXPORTED_PERCENTILES = (0.5, 0.95, 0.99)
MYSQL_PROCESS_NAME_PREFIX = "mysqld"
LOAD_GENERATOR_COMMAND_MARKER = "locust"
PROCESS_GROUPS = ("mysqld", "load_generator")
MINIMUM_CPU_MEASUREMENT_INTERVAL_SECONDS = 1.0


class LocustStatisticsCollector:
    """Exposes Locust transaction counters and current percentiles of the running test."""

    def __init__(self, environment):
        self.environment = environment

    def collect(self):
        """Build the metric families of the load generator.

        Yields:
            Prometheus metric families.
        """

        runner = self.environment.runner
        clients = GaugeMetricFamily("locust_clients", "Одночасні клієнти (з'єднання з MySQL)")
        clients.add_metric([], runner.user_count if runner else 0)
        yield clients
        stage = GaugeMetricFamily("load_test_stage", "Поточний етап плану навантаження", labels=["stage"])
        stage.add_metric([str(getattr(self.environment.shape_class, "current_stage_name", "—"))], 1)
        yield stage
        if runner is None:
            return
        transactions = CounterMetricFamily("locust_transactions", "Виконані транзакції", labels=["transaction"])
        failures = CounterMetricFamily("locust_transaction_failures", "Помилки транзакцій", labels=["transaction"])
        response_time = CounterMetricFamily("locust_response_time_milliseconds", "Сумарний час транзакцій, мс",
                                            labels=["transaction"])
        percentiles = GaugeMetricFamily("locust_current_response_time_milliseconds",
                                        "Персентилі часу транзакцій за останні ~10 с",
                                        labels=["transaction", "quantile"])
        for entry in [*runner.stats.entries.values(), runner.stats.total]:
            transactions.add_metric([entry.name], entry.num_requests)
            failures.add_metric([entry.name], entry.num_failures)
            response_time.add_metric([entry.name], entry.total_response_time)
            for percentile in EXPORTED_PERCENTILES:
                percentiles.add_metric([entry.name, str(percentile)],
                                       read_current_percentile(entry, percentile))
        yield from (transactions, failures, response_time, percentiles)


class HostResourcesCollector:
    """Exposes CPU, memory and disk usage of the laptop, MySQL and the load generator."""

    def __init__(self):
        self.logical_cpu_count = psutil.cpu_count() or 1
        self.measured_at = 0.0
        self.cached_cpu_usage = (0.0, {group: 0.0 for group in PROCESS_GROUPS},
                                 {group: 0.0 for group in PROCESS_GROUPS})
        psutil.cpu_percent(None)

    def measure_cpu_usage(self):
        """Measure CPU of the host and of the process groups, not more often than once a second.

        Returns:
            Tuple of host CPU percent, CPU percent per group and memory per group.
        """

        if time.monotonic() - self.measured_at < MINIMUM_CPU_MEASUREMENT_INTERVAL_SECONDS:
            return self.cached_cpu_usage
        host_cpu_usage = psutil.cpu_percent(None)
        cpu_by_group, memory_by_group = self.measure_process_groups()
        self.cached_cpu_usage = (host_cpu_usage, cpu_by_group, memory_by_group)
        self.measured_at = time.monotonic()
        return self.cached_cpu_usage

    def collect(self):
        """Build the metric families of the test stand resources.

        Yields:
            Prometheus metric families.
        """

        host_cpu_usage, cpu_by_group, memory_by_group = self.measure_cpu_usage()
        cpu_usage = GaugeMetricFamily("host_cpu_usage_percent", "Завантаження CPU ноутбука, %")
        cpu_usage.add_metric([], host_cpu_usage)
        memory = psutil.virtual_memory()
        memory_used = GaugeMetricFamily("host_memory_used_bytes", "Зайнята RAM, байт")
        memory_used.add_metric([], memory.total - memory.available)
        memory_total = GaugeMetricFamily("host_memory_total_bytes", "Уся RAM, байт")
        memory_total.add_metric([], memory.total)
        disk_counters = psutil.disk_io_counters()
        disk_read = CounterMetricFamily("host_disk_read_bytes", "Прочитано з диска, байт")
        disk_written = CounterMetricFamily("host_disk_written_bytes", "Записано на диск, байт")
        disk_read.add_metric([], disk_counters.read_bytes if disk_counters else 0)
        disk_written.add_metric([], disk_counters.write_bytes if disk_counters else 0)
        process_cpu = GaugeMetricFamily("process_group_cpu_usage_percent", "CPU процесів, % від усього CPU",
                                        labels=["group"])
        process_memory = GaugeMetricFamily("process_group_memory_bytes", "Пам'ять процесів (RSS), байт",
                                           labels=["group"])
        for group in PROCESS_GROUPS:
            process_cpu.add_metric([group], cpu_by_group[group])
            process_memory.add_metric([group], memory_by_group[group])
        yield from (cpu_usage, memory_used, memory_total, disk_read, disk_written, process_cpu, process_memory)

    def measure_process_groups(self):
        """Sum CPU and memory of the MySQL server and of the load generator processes.

        Returns:
            Tuple of CPU percent and resident memory per process group.
        """

        cpu_by_group = {group: 0.0 for group in PROCESS_GROUPS}
        memory_by_group = {group: 0.0 for group in PROCESS_GROUPS}
        for process in psutil.process_iter(["name"]):
            try:
                process_name = (process.info["name"] or "").lower()
                if process_name.startswith(MYSQL_PROCESS_NAME_PREFIX):
                    group = "mysqld"
                elif "python" in process_name and LOAD_GENERATOR_COMMAND_MARKER in " ".join(process.cmdline()).lower():
                    group = "load_generator"
                else:
                    continue
                cpu_by_group[group] += process.cpu_percent(None) / self.logical_cpu_count
                memory_by_group[group] += process.memory_info().rss
            except (psutil.Error, OSError):
                continue  # службовий процес MySQL може бути недоступним без прав адміністратора
        return cpu_by_group, memory_by_group


def read_current_percentile(statistics_entry, percentile):
    """Read a rolling response time percentile of one transaction.

    Args:
        statistics_entry: Locust statistics entry.
        percentile: Percentile between 0 and 1.

    Returns:
        Latency in milliseconds, zero when the rolling window is empty.
    """

    try:
        return statistics_entry.get_current_response_time_percentile(percentile) or 0
    except (ValueError, KeyError):
        return 0


def start_metrics_exporter(environment, metrics_port):
    """Publish the load test and host metrics for Prometheus.

    Args:
        environment: Locust environment of the master process.
        metrics_port: TCP port of the metrics endpoint.
    """

    registry = CollectorRegistry()
    registry.register(LocustStatisticsCollector(environment))
    registry.register(HostResourcesCollector())
    start_http_server(metrics_port, addr="0.0.0.0", registry=registry)
    print(f"[метрики] Prometheus забирає метрики з http://<хост>:{metrics_port}/metrics")
