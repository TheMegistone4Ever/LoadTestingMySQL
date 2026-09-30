import argparse
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from console_and_file_logging import configure_logging
from load_test_plan import DEFAULT_PLAN_NAME, LOAD_TEST_PLANS

LOCUSTFILE_PATH = Path(__file__).with_name("locustfile_parcel_delivery.py")
RESULTS_ROOT_DIRECTORY = Path(__file__).with_name("load_test_results")
LOG_FILE_NAME = "run_load_test.log"
WORKER_START_DELAY_SECONDS = 3
WORKER_SHUTDOWN_TIMEOUT_SECONDS = 30

logger = logging.getLogger(__name__)


def build_child_process_environment():
    """Build the environment of the Locust processes with UTF-8 output forced on.

    Returns:
        Environment of this process with PYTHONUTF8 enabled.
    """

    child_environment = os.environ.copy()
    child_environment["PYTHONUTF8"] = "1"
    return child_environment


def parse_command_line_arguments():
    """Parse the worker count and the plan overrides of the run.

    Returns:
        Parsed command line arguments.
    """

    parser = argparse.ArgumentParser(description="Load Testing БД parcel_delivery (Locust + PyMySQL)")
    parser.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) // 2),
                        help="кількість процесів-генераторів навантаження")
    parser.add_argument("--plan", type=str, default=DEFAULT_PLAN_NAME, choices=sorted(LOAD_TEST_PLANS),
                        help="requirements — етапи вимог (пункт 3); saturation — згущені сходи для пошуку перегину")
    parser.add_argument("--minutes-per-stage", type=float, default=0.0,
                        help="скоротити кожен етап до N хв, щоб перевірити стенд перед повним прогоном")
    parser.add_argument("--stages", type=str, default="",
                        help="перелік етапів через кому, наприклад load_500,stress_1000")
    parser.add_argument("--metrics-port", type=int, default=9646, help="порт експортера метрик для Prometheus")
    return parser.parse_args()


def build_master_command(arguments, results_directory):
    """Build the command line of the Locust master process.

    Args:
        arguments: Parsed command line arguments.
        results_directory: Directory of this run.

    Returns:
        Command line as a list.
    """

    master_command = [
        sys.executable, "-m", "locust", "-f", str(LOCUSTFILE_PATH), "--master", "--headless",
        "--expect-workers", str(arguments.workers),
        "--csv", str(results_directory / "locust"), "--csv-full-history",
        "--html", str(results_directory / "locust_report.html"),
        "--results-directory", str(results_directory),
        "--metrics-port", str(arguments.metrics_port),
        "--plan", arguments.plan,
        "--stop-timeout", "10",
    ]
    if arguments.minutes_per_stage:
        master_command += ["--minutes-per-stage", str(arguments.minutes_per_stage)]
    if arguments.stages:
        master_command += ["--stages", arguments.stages]
    return master_command


def start_worker_processes(workers_count, results_directory):
    """Start the Locust worker processes and send their logs to files.

    Args:
        workers_count: Number of worker processes.
        results_directory: Directory of this run.

    Returns:
        List of started processes.
    """

    worker_processes = []
    for worker_index in range(workers_count):
        log_path = results_directory / f"worker_{worker_index + 1}.log"
        worker_command = [sys.executable, "-m", "locust", "-f", str(LOCUSTFILE_PATH), "--worker",
                          "--master-host", "127.0.0.1", "--loglevel", "WARNING"]
        worker_processes.append(subprocess.Popen(worker_command, stdout=log_path.open("w", encoding="utf-8"),
                                                 stderr=subprocess.STDOUT,
                                                 env=build_child_process_environment()))
    return worker_processes


def start_master_process(arguments, results_directory):
    """Start the Locust master process with its output piped into this process.

    Args:
        arguments: Parsed command line arguments.
        results_directory: Directory of this run.

    Returns:
        Started master process.
    """

    return subprocess.Popen(build_master_command(arguments, results_directory),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=build_child_process_environment(),
                            text=True, encoding="utf-8", errors="replace", bufsize=1)


def log_master_process_output(master_process):
    """Write every output line of the master process to the console and the log file.

    Args:
        master_process: Started master process with its output piped.
    """

    for output_line in master_process.stdout:
        logger.info(output_line.rstrip())


def stop_worker_processes(worker_processes):
    """Wait for the workers to quit with the master and kill the ones that hang.

    Args:
        worker_processes: Started worker processes.
    """

    for worker_process in worker_processes:
        try:
            worker_process.wait(timeout=WORKER_SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            worker_process.terminate()


def main():
    """Run the load test plan and report where the results were written.

    Returns:
        Exit code of the Locust master process.
    """

    arguments = parse_command_line_arguments()
    directory_name = datetime.now().strftime("%Y-%m-%d_%H-%M")
    if arguments.plan != DEFAULT_PLAN_NAME:
        directory_name += f"_{arguments.plan}"
    results_directory = RESULTS_ROOT_DIRECTORY / directory_name
    results_directory.mkdir(parents=True, exist_ok=True)
    configure_logging(results_directory / LOG_FILE_NAME)
    logger.info("План «%s», master і %s воркерів, результати: %s",
                arguments.plan, arguments.workers, results_directory.resolve())
    master_process = start_master_process(arguments, results_directory)
    threading.Thread(target=log_master_process_output, args=(master_process,), daemon=True).start()
    time.sleep(WORKER_START_DELAY_SECONDS)
    worker_processes = start_worker_processes(arguments.workers, results_directory)
    try:
        master_exit_code = master_process.wait()
    except KeyboardInterrupt:
        logger.info("Зупиняю тест...")
        master_process.terminate()
        master_exit_code = master_process.wait(timeout=WORKER_SHUTDOWN_TIMEOUT_SECONDS)
    finally:
        stop_worker_processes(worker_processes)
    logger.info("Готово. Результати етапів і requirements_check.md: %s", results_directory.resolve())
    if master_exit_code != 0:
        logger.warning("Locust завершився з кодом %s — дивіться locust_exceptions.csv і locust_failures.csv",
                       master_exit_code)
    return master_exit_code


if __name__ == "__main__":
    sys.exit(main())
